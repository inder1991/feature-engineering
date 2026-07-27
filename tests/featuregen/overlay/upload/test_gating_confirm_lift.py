"""E4a Task 1 — the MEASUREMENT: AI proposes grain/as-of -> a human confirms -> what actually moves.

MEASURED on the real FTR path (see the per-test docstrings for the assertions that pin each number):

    BEFORE   1 candidate  {NEEDS_EXTERNAL_VALIDATION: 1, DESIGN_CHECKED: 0}
             rejections   {NO_POINT_IN_TIME: 9, NON_NUMERIC: 3}
    AFTER   10 candidates {NEEDS_EXTERNAL_VALIDATION: 10, DESIGN_CHECKED: 0}
             rejections   {NON_NUMERIC: 3}

So the confirm does REAL work — it retires all nine ``NO_POINT_IN_TIME`` future-leakage rejections
and no candidate afterwards carries ``GRAIN_IS_UNIQUE`` or ``TEMPORAL_IS_POPULATED`` — but the
DESIGN_CHECKED lift is **ZERO**, because ``UNIT_CONSISTENT`` / ``CURRENCY_CONSISTENT`` fire on every
bound operand of a combining op (21 of the 28 name ``cif_id`` / ``as_of_dt`` / ``txn_ts`` /
``setl_stat``, which can never carry a unit) and the FTR glossary format has no unit/currency column
at all.

This is the permanent regression guard for the whole E4 thesis, and it is deliberately end-to-end:
the catalog is built through the REAL FTR path (``read_ftr_glossary`` -> ``to_glossary_upload`` ->
``ingest_upload``) with Pass B / table synthesis ENABLED (``OVERLAY_TABLE_SYNTH=1``, exactly as the
deployed configmap sets it), so the grain / availability_time facts are PROPOSED BY THE AI — not
hand-written by the fixture. They are then disposed through the REAL governance path the
``/governance/table-facts`` routes drive (``list_open_table_fact_proposals_governance`` ->
``load_table_fact_confirmation_context`` -> ``confirm_fact`` -> ``project_verified_table_fact``);
nothing here writes ``graph_node`` directly, because a faked confirm would prove nothing.

The candidate features + their tri-state ``validation_status`` come from
``suggestions.suggest_features_for_table`` — the deterministic template engine, no hypothesis and no
LLM — which is the cheapest honest probe of the same gauntlet (``feature_assist._validate_idea``)
the governed path runs.

A NON-GENERIC source name is mandatory: a generic one like ``bank`` is created as a schema-less
TECHNICAL source elsewhere in the suite and the source-kind guard would then HOLD this FTR upload in
a full-suite run.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_ftr_adapter import _HDR, _row

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.commands import confirm_fact
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import validate_rows
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE
from featuregen.overlay.upload.suggestions import suggest_features_for_table
from featuregen.overlay.upload.table_fact_governance import (
    list_open_table_fact_proposals_governance,
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)

SOURCE = "e4a_gating_lift_ftr"
TABLE = "gate_fin_tran"
NOW = datetime(2026, 7, 27, tzinfo=UTC)
_FQN_PREFIX = "DPL_EIB_COMPLIANCE."

# column -> (concept, declared type, business term). Grounding is the router: these concepts are
# what decides which template families surface at all.
_COLUMNS = {
    "CIF_ID": ("customer_id", "varchar", "Customer Identifier"),
    "ACCT_ID": ("account_id", "varchar", "Account Identifier"),
    "TXN_AMT": ("monetary_flow", "decimal", "Transaction Amount"),
    "BAL_AMT": ("monetary_stock", "decimal", "Account Balance"),
    "AS_OF_DT": ("as_of_date", "date", "As Of Date"),
    "TXN_TS": ("event_timestamp", "timestamp", "Transaction Timestamp"),
    "TXN_CNT": ("count", "integer", "Transaction Count"),
    "CUST_HOLD": ("custody_holding", "decimal", "Custody Holding"),
    "SETL_STAT": ("settlement_status", "varchar", "Settlement Status"),
}

_UPLOADER = IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))

# What the AI synthesises for this table in Pass B. It is the MODEL's proposal — PROPOSED-only, and
# nothing here makes it operational; only the human confirm below does.
_SYNTHESIS = {"grain_columns": ["cif_id"], "as_of_column": "as_of_dt", "as_of_basis": "posted_at",
              "table_role": "fact", "primary_entity": None, "event_or_snapshot": "event"}


def _ftr_csv() -> str:
    return _HDR + "".join(
        _row(source_row=str(n), fqn=f"{_FQN_PREFIX}{TABLE.upper()}.{col}", term_name=term,
             definition=f'"{term}, recorded on {TABLE}."', data_type=declared)
        for n, (col, (_concept, declared, term)) in enumerate(_COLUMNS.items(), start=1))


@pytest.fixture
def ai_proposed_catalog(overlay_conn, monkeypatch):
    """The FTR catalog after a real ingest with Pass B ON — grain + availability_time are PROPOSED
    by the AI and sitting in the governance queue, NOT confirmed and NOT projected."""
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))
    upload = to_glossary_upload(read_ftr_glossary(_ftr_csv(), source=SOURCE))
    good = validate_rows(upload.rows, SOURCE, profile=FTR_GLOSSARY_PROFILE).good
    concepts = {content_hash(r): _COLUMNS[r.column.upper()][0] for r in good}
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": c} for h, c in concepts.items()]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": TABLE, "domain": "payments"}]}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
        # Pass B: phase-1 chunk summary (one chunk — the table is far under the 64-column bound)
        # then the phase-2 per-table synthesis.
        "table_synth_summary": FakeResponse(output={"results": [
            {"ref": f"{TABLE}#chunk0", "summary": {
                "grain_candidates": ["cif_id"], "temporal_candidates": ["as_of_dt"],
                "entity_signals": [], "event_or_snapshot": "event"}}]}),
        "table_synth": FakeResponse(output={"results": [
            {"ref": TABLE, "synthesis": _SYNTHESIS}]}),
    })
    res = ingest_upload(overlay_conn, SOURCE, upload.rows, actor=_UPLOADER, now=NOW,
                        client=client, glossary=upload)
    assert res.status == "ingested", res.status
    return SOURCE


def _payload(conn) -> dict:
    out = suggest_features_for_table(conn, catalog_source=SOURCE, table=TABLE)
    assert out["table_known"], "the fixture catalog does not hold the table"
    return out


def _cards(conn) -> list[dict]:
    return [s for g in _payload(conn)["groups"] for s in g["suggestions"]]


def _rejection_codes(conn) -> Counter:
    return Counter(r["code"] for r in _payload(conn)["rejections"])


def _status_counts(conn) -> Counter:
    return Counter(s["validation_status"] for s in _cards(conn))


def _requirement_codes(conn) -> Counter:
    return Counter(r["code"] for s in _cards(conn) for r in s["requirements"])


def _requirement_operands(conn) -> Counter:
    """``code@column`` for every minted requirement — the diagnosis behind the headline count: it is
    what shows WHICH operand a still-firing requirement names."""
    return Counter(f"{r['code']}@{r['operand'][-1].rsplit('.', 1)[-1]}"
                   for s in _cards(conn) for r in s["requirements"])


def _confirm_ai_table_facts(conn, admin) -> list[str]:
    """A human disposes of every open AI table-fact proposal for this source through the REAL
    governance surface — the same read model + confirm command + synchronous projection the
    ``/governance/table-facts/{fact_key}/confirm`` route drives. Returns the confirmed fact types."""
    queued = list_open_table_fact_proposals_governance(conn, SOURCE)
    assert queued, ("Pass B proposed nothing to the table-fact governance queue — the AI-proposal "
                    "link of the E4 chain is broken")
    confirmed: list[str] = []
    for view in queued:
        if view["table"] != TABLE:
            continue
        ctx = load_table_fact_confirmation_context(conn, view["fact_key"])
        res = confirm_fact(conn, Command(
            "confirm_fact", "overlay_fact", None,
            {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
             "target_event_id": ctx["target_event_id"]},
            admin, f"confirm-{ctx['target_event_id']}"))
        assert res.accepted, f"{ctx['fact_type']}: {res.denied_reason}"
        landed = project_verified_table_fact(conn, SOURCE, ctx["ref"], ctx["fact_type"], now=NOW)
        assert landed == "projected", (
            f"confirming {ctx['fact_type']} did not project onto graph_node ({landed}) — the "
            "confirm->project link of the E4 chain is broken")
        confirmed.append(ctx["fact_type"])
    return confirmed


def test_the_ai_proposes_grain_and_as_of_and_a_human_can_see_them_queued(overlay_conn,
                                                                        ai_proposed_catalog):
    """Link 1 + 2 of the chain, pinned separately so a break is diagnosable: Pass B PROPOSED both
    gating facts and the governance read model actually lists them as open proposals — stamped as
    LLM synthesis, never as profiled proof."""
    queued = list_open_table_fact_proposals_governance(overlay_conn, SOURCE)
    mine = {v["fact_type"]: v for v in queued if v["table"] == TABLE}
    assert set(mine) == {"grain", "availability_time"}
    assert all(v["status"] == "PROPOSED" for v in mine.values())
    assert all(v["origin"] == "llm_proposed_not_profiled" for v in mine.values())
    assert mine["grain"]["proposed_value"] == {"columns": ["cif_id"], "is_unique": True}
    assert mine["availability_time"]["proposed_value"] == {"column": "as_of_dt",
                                                           "basis": "posted_at"}
    # and NOTHING is operational yet — the proposal alone must never move the flat flags
    assert overlay_conn.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND kind = 'column' AND (is_grain OR is_as_of)", (SOURCE, TABLE)).fetchone()[0] == 0


def test_confirming_the_ai_grain_and_asof_lifts_features_past_the_gating_checks(overlay_conn,
                                                                               ai_proposed_catalog,
                                                                               capsys):
    """THE MEASUREMENT. The AI proposes grain + availability, a human confirms them in the real
    governance surface, and the candidate set is re-measured.

    MEASURED (run with ``-s`` to print it): the confirm takes the table from **1 candidate to 10**.
    The nine it adds were HARD-REJECTED beforehand — a windowed feature on a table with no
    governed-VERIFIED as-of column is a ``NO_POINT_IN_TIME`` future-leakage reject, not a
    requirement — so this is the real, load-bearing work the one-click confirm does. Afterwards NO
    candidate carries ``GRAIN_IS_UNIQUE`` or ``TEMPORAL_IS_POPULATED``: the C1 governed read
    (``read_operational_value`` over ``is_grain`` / ``is_as_of`` + their ``*_fact_event_id``)
    resolves off the confirm alone, with no re-upload.

    The DESIGN_CHECKED lift is measured by its own test below — it is ZERO, for a reason that is
    NOT in this chain."""
    before, before_codes = _status_counts(overlay_conn), _requirement_codes(overlay_conn)
    before_rejects = _rejection_codes(overlay_conn)
    assert before["NEEDS_EXTERNAL_VALIDATION"] > 0, (
        f"nothing needed validation before the confirm — the requirements do not fire ({before})")
    assert before_rejects["NO_POINT_IN_TIME"] > 0, (
        "the table already had a governed as-of before the confirm — the fixture is not measuring "
        "the confirm")

    admin = mint_test_identity(subject="user:e4a-admin", role_claims=("platform-admin",))
    assert sorted(_confirm_ai_table_facts(overlay_conn, admin)) == ["availability_time", "grain"]

    after, after_codes = _status_counts(overlay_conn), _requirement_codes(overlay_conn)
    after_rejects = _rejection_codes(overlay_conn)
    with capsys.disabled():
        print(f"\nE4a gating lift on {SOURCE}.{TABLE}"
              f"\n  BEFORE candidates {dict(before)} rejections {dict(before_rejects)}"
              f"\n         requirements {dict(before_codes)}"
              f"\n  AFTER  candidates {dict(after)} rejections {dict(after_rejects)}"
              f"\n         requirements {dict(after_codes)}"
              f"\n  cleared requirement codes: {sorted(set(before_codes) - set(after_codes))}"
              f"\n  cleared rejection codes:   {sorted(set(before_rejects) - set(after_rejects))}"
              f"\n  still firing, by operand:  {dict(_requirement_operands(overlay_conn))}")

    assert sum(after.values()) > sum(before.values()), (
        "confirming the AI's grain/as-of cleared no gating check — the E4 chain is broken "
        f"(before {dict(before)}, after {dict(after)})")
    assert not after_rejects["NO_POINT_IN_TIME"], (
        "the confirmed as-of did not lift the future-leakage reject — the confirm->project->C1 "
        "link is broken")
    assert not ({"GRAIN_IS_UNIQUE", "TEMPORAL_IS_POPULATED"} & set(after_codes)), (
        "the confirmed grain/as-of is projected but C1 still does not read it as governed — the "
        f"gauntlet link is broken ({dict(after_codes)})")


def test_the_confirm_clears_the_gating_checks_and_mints_no_new_requirement(overlay_conn,
                                                                          ai_proposed_catalog):
    """WHICH codes the confirm retires, pinned apart from the headline so a future change that lifts
    the number for an UNRELATED reason — or that clears a gating field through a new flat-presence
    surface instead of the governed read — is still visible. A confirm must never TIGHTEN either."""
    before_codes = set(_requirement_codes(overlay_conn))
    _confirm_ai_table_facts(overlay_conn,
                            mint_test_identity(subject="user:e4a-admin",
                                               role_claims=("platform-admin",)))
    after_codes = set(_requirement_codes(overlay_conn))
    assert not ({"GRAIN_IS_UNIQUE", "TEMPORAL_IS_POPULATED"} & after_codes)
    assert not (after_codes - before_codes), f"the confirm minted new requirements: {after_codes}"


def test_the_design_checked_lift_is_zero_because_unit_currency_fires_on_every_operand(
        overlay_conn, ai_proposed_catalog):
    """THE HONEST HEADLINE: confirming the AI's grain/as-of moves **zero** features to
    DESIGN_CHECKED, and the blocker is NOT the E4a chain.

    Every surviving candidate is stopped by ``UNIT_CONSISTENT`` / ``CURRENCY_CONSISTENT``, which
    ``_validate_idea`` mints for EVERY bound operand of a combining op — the grain key, the as-of
    date and a status string included (measured: 21 of the 28 unit requirements name ``cif_id``,
    ``as_of_dt``, ``txn_ts`` or ``setl_stat``; only 7 name an actual measure). A real FTR glossary
    export carries no unit or currency column AT ALL, so those values are always NULL on this path.

    Consequence for the programme: E4b as scoped (govern ``unit``/``currency``) would still lift
    this number to zero, because no one can attest a "unit" for a customer id or a date. This test
    pins that reality; when the operand selection or the unit gate changes, it MUST fail and be
    re-measured rather than quietly passing."""
    _confirm_ai_table_facts(overlay_conn,
                            mint_test_identity(subject="user:e4a-admin",
                                               role_claims=("platform-admin",)))
    counts = _status_counts(overlay_conn)
    assert counts["DESIGN_CHECKED"] == 0
    assert set(_requirement_codes(overlay_conn)) == {"UNIT_CONSISTENT", "CURRENCY_CONSISTENT"}
    # the unit/currency checks name NON-MEASURE operands — the precise reason the lift is zero
    non_measures = {op for op in _requirement_operands(overlay_conn)
                    if op.rsplit("@", 1)[-1] in ("cif_id", "as_of_dt", "txn_ts", "setl_stat")}
    assert non_measures, "unit/currency no longer fires on non-measure operands — re-measure E4a"
