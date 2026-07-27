"""E4a Task 3 — surface the AI's unit suggestion, and let a human confirm it.

The loop the two earlier tasks left open:

* T1 narrowed ``UNIT_CONSISTENT`` / ``CURRENCY_CONSISTENT`` to the operands where units can actually
  mix (DESIGN_CHECKED 0 -> 5 of 10; E4b's operand roles later took it to 10 of 10 by dropping the
  ride-along key/timestamp operands, so the FTR measurements below are re-recorded against a
  two-declared-MEASURE probe — the case the check actually exists for);
* T2 made the LLM PROPOSE a unit as ``llm/proposed`` **evidence** — deliberately inert: it never
  reaches ``graph_node.unit``, the only column the gauntlet reads, so it cannot clear the check.

This file pins the closing half. **Half A** surfaces the AI's proposal ON the requirement (typed,
registry-validated ``params``) so a reviewer reads *"unit not confirmed — AI suggests AED"* instead
of a bare "unit unknown", and pins that the suggestion survives BOTH serializers. **Half B** makes a
human able to CONFIRM it (``human_editable`` + a display projection), which is what finally lets the
requirement clear.

The safety claims are re-asserted here, not assumed:

* (c) an ``llm/proposed`` unit ALONE still does NOT clear the check — the LLM is absent from
  ``_MEASURE_ANNOTATION``'s display AND operational rules, so it can never win resolution;
* (d) THE PROJECTION-WIPE TEST — giving ``unit`` a display projection makes the resolver
  authoritative over a flat column ``build_graph`` ALSO populates from the file. A ref whose only
  unit evidence is ``llm/proposed`` resolves ``display_value=None``; that ``None`` must NEVER be
  projected over a real declared unit;
* (e) the ``MIXED_UNITS`` / ``MIXED_CURRENCY`` HARD rejects still fire.

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

from featuregen.api.feature_serialize import serialize_feature_idea_v2
from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.commands import confirm_fact
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import read_active_field_evidence, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows
from featuregen.overlay.upload.contract._serial import (
    requirements_from_json,
    requirements_to_json,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_assist import RejectCode, _validate_idea
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE
from featuregen.overlay.upload.suggestions import suggest_features_for_table
from featuregen.overlay.upload.table_fact_governance import (
    list_open_table_fact_proposals_governance,
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)

NOW = datetime(2026, 7, 27, tzinfo=UTC)
FRESH = timedelta(hours=24)

SOURCE = "e4a_unit_confirm_ftr"
TABLE = "loop_fin_tran"
_SCHEMA = "DPL_EIB_COMPLIANCE"

# column -> (concept, declared type, business term). The concept is the router: it decides which
# templates surface AND (T2) which columns the AI is asked for a unit.
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
_MEASURES = ("TXN_AMT", "BAL_AMT", "TXN_CNT", "CUST_HOLD")

_UPLOADER = IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))
_ADMIN = mint_test_identity(subject="user:e4a-loop-admin", role_claims=("platform-admin",))

_SYNTHESIS = {"grain_columns": ["cif_id"], "as_of_column": "as_of_dt", "as_of_basis": "posted_at",
              "table_role": "fact", "primary_entity": None, "event_or_snapshot": "event"}


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _fresh(conn, source: str) -> None:
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = %s", (source, NOW, NOW))


# ══ Half A — the suggestion rides on the requirement ═══════════════════════════════════════════════
# A cheap, direct probe of the gauntlet (`_validate_idea`) — no LLM, no ingest.

_MINI = "e4a_unit_mini_ftr"


def _mini_catalog(conn, *, units: dict[str, str] | None = None) -> dict[str, str]:
    """Two MEASURES + a grain in one table (a genuine COMBINING op, so the unit question is asked),
    with ``units[column]`` written as ``llm/proposed`` evidence at the schema-preserving ref."""
    rows = [
        CanonicalRow(_MINI, "txns", "cif_id", "varchar", is_grain=True),
        CanonicalRow(_MINI, "txns", "txn_amt", "numeric"),
        CanonicalRow(_MINI, "txns", "fee_amt", "numeric"),
    ]
    build_graph(conn, _MINI, rows)
    _fresh(conn, _MINI)
    for column, unit in (units or {}).items():
        ref = normalize_ref(_MINI, None, "txns", column)
        record_field_evidence(
            conn, logical_ref=ref, field_name="unit", proposed_value=unit,
            producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
            producer_ref="e4a-drafter", source_snapshot_id="snap", input_hash=f"llm-unit-{column}")
        record_field_evidence(
            conn, logical_ref=ref, field_name="currency", proposed_value=unit,
            producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
            producer_ref="e4a-drafter", source_snapshot_id="snap", input_hash=f"llm-ccy-{column}")
    return {c: f"public.txns.{c}" for c in ("cif_id", "txn_amt", "fee_amt")}


def _combining_idea(conn, refs: dict[str, str]):
    known = set(refs.values())
    raw = {"name": "fee_ratio", "derives_from": [refs["txn_amt"], refs["fee_amt"], refs["cif_id"]],
           "aggregation": "sum", "grain_table": "txns"}
    idea, rej = _validate_idea(conn, raw, known, {r: {_MINI} for r in known}, None, NOW, FRESH)
    assert rej is None, rej
    return idea


def _req(idea, code: str, column: str):
    return next(r for r in idea.requirements
                if r.code == code and r.operand[1].endswith(f".{column}"))


def test_the_requirement_carries_the_ai_suggested_unit(db):
    """(a) The reviewer's card must read "unit not confirmed — AI suggests AED", so the AI's
    ``llm/proposed`` value rides on the requirement as a REGISTRY-TYPED param."""
    refs = _mini_catalog(db, units={"txn_amt": "AED"})
    idea = _combining_idea(db, refs)

    unit_req = _req(idea, "UNIT_CONSISTENT", "txn_amt")
    assert dict(unit_req.params)["suggested_unit"] == "AED"
    ccy_req = _req(idea, "CURRENCY_CONSISTENT", "txn_amt")
    assert dict(ccy_req.params)["suggested_currency"] == "AED"

    # ...and an operand the AI did NOT answer carries no suggestion (never a fabricated one)
    assert "suggested_unit" not in dict(_req(idea, "UNIT_CONSISTENT", "fee_amt").params)


def test_the_suggestion_survives_both_serializers(db):
    """(a) The suggestion is useless if it dies on the wire. It must survive the contract snapshot
    round-trip (``contract/_serial.py``) AND reach the API (``api/feature_serialize.py``), which
    dropped ``params``/``schema_version`` entirely."""
    refs = _mini_catalog(db, units={"txn_amt": "AED"})
    idea = _combining_idea(db, refs)

    # 1. the contract snapshot round-trip re-materializes a REGISTRY-VALID requirement
    back = requirements_from_json(requirements_to_json(idea.requirements))
    restored = next(r for r in back
                    if r.code == "UNIT_CONSISTENT" and r.operand[1].endswith(".txn_amt"))
    assert dict(restored.params)["suggested_unit"] == "AED"
    assert restored.schema_version == _req(idea, "UNIT_CONSISTENT", "txn_amt").schema_version

    # 2. the API v2 response actually carries it to the UI
    payload = serialize_feature_idea_v2(idea)
    wire = next(r for r in payload["requirements"]
                if r["code"] == "UNIT_CONSISTENT" and r["operand"][1].endswith(".txn_amt"))
    assert ["suggested_unit", "AED"] in wire["params"]
    assert wire["schema_version"] == _req(idea, "UNIT_CONSISTENT", "txn_amt").schema_version
    # a requirement with NO params stays byte-identical to the pre-E4a shape (additive emission)
    plain = next(r for r in payload["requirements"]
                 if r["code"] == "UNIT_CONSISTENT" and r["operand"][1].endswith(".fee_amt"))
    assert plain == {"code": "UNIT_CONSISTENT", "operand": list(plain["operand"]),
                     "detail": plain["detail"], "schema_version": plain["schema_version"]}


def test_mixed_units_and_mixed_currency_still_hard_reject(db):
    """(e) The narrowing and the suggestion are both about the UNKNOWN case. A positive
    CONTRADICTION between two declared units is still an outright REJECT — never a requirement."""
    for field, code in (("unit", RejectCode.MIXED_UNITS), ("currency", RejectCode.MIXED_CURRENCY)):
        source = f"{_MINI}_{field}"
        rows = [
            CanonicalRow(source, "txns", "cif_id", "varchar", is_grain=True),
            CanonicalRow(source, "txns", "txn_amt", "numeric", **{field: "AED"}),
            CanonicalRow(source, "txns", "fee_amt", "numeric", **{field: "fils"}),
        ]
        build_graph(db, source, rows)
        _fresh(db, source)
        refs = [f"public.txns.{c}" for c in ("txn_amt", "fee_amt", "cif_id")]
        raw = {"name": "x", "derives_from": refs, "aggregation": "sum", "grain_table": "txns"}
        idea, rej = _validate_idea(db, raw, set(refs), {r: {source} for r in refs}, None, NOW, FRESH)
        assert idea is None and rej.code == code, (field, idea, rej)


# ══ (d) THE PROJECTION-WIPE TEST ═══════════════════════════════════════════════════════════════════


def test_an_ai_unit_proposal_never_wipes_a_source_declared_graph_node_unit(db):
    """(d) THE HAZARD, PINNED. Giving ``unit`` a DISPLAY projection makes the resolver authoritative
    over a flat column ``build_graph`` ALSO writes from the file. The LLM is excluded from
    ``_MEASURE_ANNOTATION``'s display rule, so a ref whose ONLY unit evidence is ``llm/proposed``
    resolves ``display_value=None`` — and an unscoped projection would write that ``None`` straight
    over a real declared unit, DESTROYING catalog truth on the very upload that added the AI's help.

    A glossary upload declares the unit in ``graph_node`` WITHOUT writing source ``unit`` evidence
    (``_SOURCE_FIELDS`` has no unit), so there is nothing for the resolver to re-derive it from —
    the wipe would be permanent until a re-upload."""
    source = "e4a_unit_wipe_ftr"
    build_graph(db, source, [
        CanonicalRow(source, "txns", "cif_id", "varchar", is_grain=True),
        CanonicalRow(source, "txns", "txn_amt", "numeric", unit="AED", currency="AED"),
    ])
    ref = normalize_ref(source, None, "txns", "txn_amt")
    for field in ("unit", "currency"):
        record_field_evidence(
            db, logical_ref=ref, field_name=field, proposed_value="fils",
            producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
            producer_ref="e4a-drafter", source_snapshot_id="snap", input_hash=f"llm-{field}-wipe")

    resolve_and_project(db, source=source, logical_refs=[ref], now=NOW)

    row = db.execute(
        "SELECT unit, currency FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (source, "public.txns.txn_amt")).fetchone()
    assert row == ("AED", "AED"), (
        "an llm/proposed unit WIPED the file-declared graph_node.unit — the resolver must never "
        "project a None over a value it did not author")


# ══ Half B — a human confirms the AI's unit and the requirement clears ═════════════════════════════


def _ftr_csv() -> str:
    return _HDR + "".join(
        _row(source_row=str(n), fqn=f"{_SCHEMA}.{TABLE.upper()}.{col}", term_name=term,
             definition=f'"{term}, recorded on {TABLE}."', data_type=declared)
        for n, (col, (_concept, declared, term)) in enumerate(_COLUMNS.items(), start=1))


def _ref(column: str) -> str:
    return normalize_ref(SOURCE, _SCHEMA, TABLE, column)


@pytest.fixture
def ai_proposed_catalog(overlay_conn, monkeypatch):
    """The real FTR catalog after a real ingest with Pass B ON: the AI proposed grain + as-of AND a
    unit/currency for every measure, and a human has confirmed the two table facts (so the features
    are past the grain/temporal gates and the ONLY thing left is the unit question)."""
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    _seal()
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
        "overlay.enrich.unit": FakeResponse(output={"results": [
            {"ref": h, "unit": "AED", "currency": "AED"} for h, c in concepts.items()
            if c in ("monetary_flow", "monetary_stock", "custody_holding", "count")]}),
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
    _confirm_ai_table_facts(overlay_conn)
    return SOURCE


def _confirm_ai_table_facts(conn) -> None:
    queued = list_open_table_fact_proposals_governance(conn, SOURCE)
    assert queued, "Pass B proposed nothing — the fixture is not exercising the gauntlet"
    for view in queued:
        if view["table"] != TABLE:
            continue
        ctx = load_table_fact_confirmation_context(conn, view["fact_key"])
        res = confirm_fact(conn, Command(
            "confirm_fact", "overlay_fact", None,
            {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
             "target_event_id": ctx["target_event_id"]}, _ADMIN,
            f"confirm-{ctx['target_event_id']}"))
        assert res.accepted, f"{ctx['fact_type']}: {res.denied_reason}"
        assert project_verified_table_fact(
            conn, SOURCE, ctx["ref"], ctx["fact_type"], now=NOW) == "projected"


def _cards(conn) -> list[dict]:
    out = suggest_features_for_table(conn, catalog_source=SOURCE, table=TABLE)
    assert out["table_known"]
    return [s for g in out["groups"] for s in g["suggestions"]]


def _status_counts(conn) -> Counter:
    return Counter(s["validation_status"] for s in _cards(conn))


def _requirement_operands(conn) -> Counter:
    return Counter(f"{r['code']}@{r['operand'][-1].rsplit('.', 1)[-1]}"
                   for s in _cards(conn) for r in s["requirements"])


def _two_measure_probe(conn) -> Counter:
    """The gauntlet over a feature that genuinely COMBINES this table's two monetary measures.

    Since E4b every RECIPE candidate this FTR sample grounds binds at most ONE declared measure, so
    the suggestion path asks no unit question here at all — correctly, since a lone measure cannot
    mix. That is a fact about the sample, not about the safety rule, so the claims below are asserted
    where the rule genuinely applies: two operands the recipe corpus DECLARES as measures
    (``flow_col`` / ``stock_col``). The role rule therefore cannot be the reason a question fires or
    does not — the only variable left is the unit evidence itself."""
    refs = [f"public.{TABLE}.txn_amt", f"public.{TABLE}.cust_hold"]
    idea, rej = _validate_idea(
        conn, {"name": "txn_over_hold", "derives_from": refs, "aggregation": "ratio_30d",
               "grain_table": TABLE},
        set(refs), {ref: {SOURCE} for ref in refs}, None, NOW, FRESH,
        operand_roles=((refs[0], "flow_col"), (refs[1], "stock_col")))
    assert rej is None, f"the two-measure probe was rejected outright: {rej and rej.code}"
    return Counter(f"{r.code}@{r.operand[-1].rsplit('.', 1)[-1]}" for r in idea.requirements
                   if r.code in ("UNIT_CONSISTENT", "CURRENCY_CONSISTENT"))


def _confirm_ai_measure_annotation(conn, column: str, field: str) -> dict:
    """A human confirms the AI's proposal for ONE measure annotation through the REAL generic
    field-correction command — the same path the ``/catalog/assets/.../fields/{field}/decisions``
    route drives (``human_editable`` opt-in + CAS + four-eyes + append-only)."""
    object_ref = f"public.{TABLE}.{column}"
    llm = [e for e in read_active_field_evidence(conn, _ref(column), field) if e.producer == "llm"]
    assert llm, f"no AI {field} proposal at {column} — nothing to confirm"
    cas = read_field_cas(conn, source=SOURCE, object_ref=object_ref, field=field)
    return apply_field_correction(
        conn, source=SOURCE, object_ref=object_ref, field=field, action="confirm_existing",
        actor=_ADMIN, idempotency_key=f"e4a-confirm-{field}-{column}",
        expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"],
        selected_evidence_ids=[e.evidence_id for e in llm])


def test_an_ai_proposed_unit_alone_still_does_not_clear_the_check(overlay_conn, ai_proposed_catalog):
    """(c) T2's LOAD-BEARING SAFETY TEST, re-asserted on T3's code. The AI has proposed AED for
    every measure and the proposals are stored, governed and visible — and ``UNIT_CONSISTENT`` on
    those very operands must STILL FIRE, because only a source-attested or human-confirmed value
    ever reaches ``graph_node.unit``.

    If this goes green because the requirement vanished, an unreviewed model guess is silently
    certifying that two measures are unit-compatible."""
    assert [e for e in read_active_field_evidence(overlay_conn, _ref("txn_amt"), "unit")
            if e.producer == "llm"], "no AI proposal — nothing is being tested"
    node = overlay_conn.execute(
        "SELECT unit, currency FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (SOURCE, f"public.{TABLE}.txn_amt")).fetchone()
    assert node == (None, None), f"an llm/proposed value reached graph_node: {node}"

    reqs = _two_measure_probe(overlay_conn)
    assert reqs["UNIT_CONSISTENT@txn_amt"] > 0, (
        "an llm/proposed unit CLEARED UNIT_CONSISTENT — an AI guess now certifies unit safety")
    assert reqs["CURRENCY_CONSISTENT@txn_amt"] > 0


def test_a_human_confirm_of_the_ai_unit_clears_the_requirement(overlay_conn, ai_proposed_catalog,
                                                               capsys):
    """(b) THE LOOP CLOSES. A human confirms the AI's unit + currency in one action per field,
    through the real correction command — no 403 (``_MEASURE_ANNOTATION`` is now ``human_editable``),
    the HUMAN/CONFIRMED value PROJECTS into ``graph_node.unit``/``.currency`` (the only columns the
    gauntlet reads), and every requirement on those operands CLEARS.

    **Re-recorded after E4b (operand roles).** The ride-along ceiling this test used to measure is
    GONE: an operand whose TEMPLATE-DECLARED role is a key or a timestamp is no longer counted as a
    measure, so ``txn_ts`` / ``acct_id`` / ``setl_stat`` are never asked and every card on this
    sample already sits at DESIGN_CHECKED — **22 -> 0 questions, DESIGN_CHECKED 5 -> 10 of 10**,
    before this loop runs at all.

    That leaves nothing for the confirm to clear ON THE CARDS, so the loop is measured where the
    question genuinely exists: a two-DECLARED-MEASURE probe over the SAME catalog goes from
    NEEDS_EXTERNAL_VALIDATION (unit + currency unknown on both measures) to DESIGN_CHECKED (no
    requirement left) across the four one-click confirms. The card-level counts are still pinned
    (not bounded) so any movement in either direction is a visible edit."""
    before = _status_counts(overlay_conn)
    before_reqs = _requirement_operands(overlay_conn)
    before_probe = _two_measure_probe(overlay_conn)
    assert before_probe["UNIT_CONSISTENT@txn_amt"] > 0

    confirms = 0
    for column in (c.lower() for c in _MEASURES):
        for field in ("unit", "currency"):
            if not [e for e in read_active_field_evidence(overlay_conn, _ref(column), field)
                    if e.producer == "llm"]:
                continue
            res = _confirm_ai_measure_annotation(overlay_conn, column, field)
            assert res["accepted"], res            # NOT a 403 — the field is human-editable now
            assert res["body"]["projected"], (column, field)
            confirms += 1

    node = overlay_conn.execute(
        "SELECT unit, currency FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (SOURCE, f"public.{TABLE}.txn_amt")).fetchone()
    assert node == ("AED", "AED"), f"the human-confirmed unit did not project: {node}"

    after = _status_counts(overlay_conn)
    after_reqs = _requirement_operands(overlay_conn)
    print(f"\nE4a T3 on {SOURCE}.{TABLE} — {confirms} one-click confirms"
          f"\n  DESIGN_CHECKED {before['DESIGN_CHECKED']} -> {after['DESIGN_CHECKED']} "
          f"of {sum(after.values())}"
          f"\n  questions {sum(before_reqs.values())} -> {sum(after_reqs.values())}"
          f"\n  before: {dict(before_reqs)}\n  after:  {dict(after_reqs)}")

    # THE LOOP ITSELF, measured where the question genuinely exists: the two-declared-measure probe
    # asked for four things before the confirms and asks for nothing after.
    after_probe = _two_measure_probe(overlay_conn)
    assert set(before_probe) == {"UNIT_CONSISTENT@txn_amt", "CURRENCY_CONSISTENT@txn_amt",
                                 "UNIT_CONSISTENT@cust_hold", "CURRENCY_CONSISTENT@cust_hold"}, (
        f"re-measure: the probe's open questions changed ({dict(before_probe)})")
    assert dict(after_probe) == {}, (
        f"the human confirm did not clear the probe's unit questions — the loop is still open: "
        f"{dict(after_probe)}")

    # ...and the card-level ceiling E4b removed: nothing on this sample is asked at all any more, so
    # the tri-state is already at its terminal state before AND after the confirm.
    assert sum(before_reqs.values()) == 0 and sum(after_reqs.values()) == 0, (
        f"re-measure the headline: {sum(before_reqs.values())} -> {sum(after_reqs.values())}")
    assert dict(before) == {"DESIGN_CHECKED": 10}
    assert dict(after) == {"DESIGN_CHECKED": 10}


def test_confirming_the_ai_unit_takes_a_feature_all_the_way_to_design_checked(db):
    """(b) THE TERMINAL STATE, proven where the operands are all genuine MEASURES. Two measure
    columns, an AI unit + currency proposal on each, a human confirm on each — and the feature
    lands on ``DESIGN_CHECKED`` with NO requirements left — the loop's payoff on a purpose-built
    catalog, independent of whatever the FTR sample happens to ground."""
    refs = _mini_catalog(db, units={"txn_amt": "AED", "fee_amt": "AED"})

    def _idea():
        """Two MEASURES averaged — a genuine combining op whose ONLY open question is the unit, so
        the tri-state actually turns on the confirm (numeric types are declared; no grain/window)."""
        known = {refs["txn_amt"], refs["fee_amt"]}
        raw = {"name": "avg_amounts", "derives_from": sorted(known), "aggregation": "avg"}
        idea, rej = _validate_idea(db, raw, known, {r: {_MINI} for r in known}, None, NOW, FRESH)
        assert rej is None, rej
        return idea

    before = _idea()
    assert before.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert {r.code for r in before.requirements} == {"UNIT_CONSISTENT", "CURRENCY_CONSISTENT"}

    for column in ("txn_amt", "fee_amt"):
        for field in ("unit", "currency"):
            object_ref = f"public.txns.{column}"
            evidence = read_active_field_evidence(db, normalize_ref(_MINI, None, "txns", column),
                                                  field)
            cas = read_field_cas(db, source=_MINI, object_ref=object_ref, field=field)
            res = apply_field_correction(
                db, source=_MINI, object_ref=object_ref, field=field, action="confirm_existing",
                actor=_ADMIN, idempotency_key=f"mini-{field}-{column}",
                expected_latest_decision_id=cas["latest_decision_id"],
                expected_evidence_set_hash=cas["evidence_set_hash"],
                expected_policy_version=cas["policy_version"],
                selected_evidence_ids=[e.evidence_id for e in evidence])
            assert res["accepted"] and res["body"]["projected"], (column, field, res)

    after = _idea()
    assert after.requirements == (), after.requirements
    assert after.validation_status == "DESIGN_CHECKED"


def test_the_confirmed_unit_survives_a_re_upload(overlay_conn, ai_proposed_catalog, monkeypatch):
    """A human confirmation is DURABLE: ``build_graph`` recreates ``graph_node`` on every upload, so
    the projection must be re-derived from the surviving HUMAN/CONFIRMED evidence — not lost."""
    _confirm_ai_measure_annotation(overlay_conn, "txn_amt", "unit")
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    upload = to_glossary_upload(read_ftr_glossary(_ftr_csv(), source=SOURCE))
    res = ingest_upload(overlay_conn, SOURCE, upload.rows, actor=_UPLOADER,
                        now=NOW + timedelta(hours=1), glossary=upload)
    assert res.status == "ingested", res.status

    assert overlay_conn.execute(
        "SELECT unit FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (SOURCE, f"public.{TABLE}.txn_amt")).fetchone()[0] == "AED"


def test_a_non_measure_field_is_still_refused_by_the_generic_command(overlay_conn,
                                                                     ai_proposed_catalog):
    """Opening ``unit``/``currency`` must not open the fields that deliberately keep their DEDICATED
    command: the physical/logical TYPE and ``sensitivity`` still 403."""
    from featuregen.overlay.upload.field_correction import FieldCorrectionError

    for field in ("data_type", "sensitivity", "logical_representation"):
        with pytest.raises(FieldCorrectionError) as exc:
            apply_field_correction(
                overlay_conn, source=SOURCE, object_ref=f"public.{TABLE}.txn_amt", field=field,
                action="confirm_existing", actor=_ADMIN, idempotency_key=f"e4a-403-{field}",
                expected_latest_decision_id=None, expected_evidence_set_hash="x",
                expected_policy_version="y", selected_evidence_ids=["e"])
        assert exc.value.status_code == 403, field


def test_a_source_declared_unit_is_still_single_party_unconfirmable(overlay_conn):
    """The four-eyes bar the generic command already carries is not weakened by opening the field:
    SOURCE-declared evidence still cannot be single-party ``confirm_existing``-ed (the file's
    uploader is unverifiable), so it must go through propose_override -> confirm_override."""
    _seal()
    source = "e4a_unit_source_ftr"
    build_graph(overlay_conn, source, [
        CanonicalRow(source, "txns", "txn_amt", "numeric", unit="USD")])
    ref = normalize_ref(source, None, "txns", "txn_amt")
    eid = record_field_evidence(
        overlay_conn, logical_ref=ref, field_name="unit", proposed_value="USD",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="snap", source_snapshot_id="snap", input_hash="src-unit")
    cas = read_field_cas(overlay_conn, source=source, object_ref="public.txns.txn_amt", field="unit")
    res = apply_field_correction(
        overlay_conn, source=source, object_ref="public.txns.txn_amt", field="unit",
        action="confirm_existing", actor=_ADMIN, idempotency_key="e4a-source-confirm",
        expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], selected_evidence_ids=[eid])
    assert res["accepted"] is False and res["status_code"] == 403


def test_the_llm_is_still_absent_from_the_measure_annotation_rules():
    """THE STRUCTURAL GUARANTEE, asserted directly on the policy. ``human_editable`` is an opt-in to
    the CORRECTION command; it changes no authority. The LLM must remain absent from BOTH rules —
    that is what keeps a silent clear structurally impossible rather than merely guarded."""
    from featuregen.overlay.field_authority import AnyOf, HasEvidence
    from featuregen.overlay.upload.field_policies import policy_for

    source_or_human = AnyOf((
        HasEvidence(EvidenceProducer.SOURCE, AssertionStrength.ATTESTED),
        HasEvidence(EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)))
    for field in ("unit", "currency"):
        policy = policy_for(field)
        assert policy is not None and policy.human_editable is True, field
        assert policy.display_rule == source_or_human, field
        assert policy.operational_rule == source_or_human, field
