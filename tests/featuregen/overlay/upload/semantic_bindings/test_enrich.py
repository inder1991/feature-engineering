"""D3 — the OPTIONAL LLM SELECTION layer for semantic bindings (``overlay.semantic_bindings``).

Real-DB tests with a FAKE LLM client (no real provider). The client reads the presented
candidate_ids straight out of the already-egress-safe payload and returns a selection response, so a
test can prove select-only behaviour, the no-unsafe-egress payload contract, the bounds → partial/
failed fail-soft, the SEPARATE failure domain (Pass B untouched), the no-persist-before-``llm_call_ref``
ordering, and confidence-as-evidence-never-authority.

``conn`` is the migrated PG connection (writes roll back on teardown); ``catalog`` (overlay conftest)
registers a StubCatalog so a propose→fact command resolves authority.
"""
from __future__ import annotations

import json

import psycopg
import pytest
from tests.featuregen._helpers import mint_test_service_identity

from featuregen.intake.llm import PROVIDER_NON_RETRYABLE, PROVIDER_OK, LLMResult
from featuregen.overlay.upload.column_view import ColumnMetadataView, TableMetadataView
from featuregen.overlay.upload.enrich_config import SemanticBindingBounds
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.semantic_bindings import (
    PassCIdentifier,
    enrich_semantic_bindings,
    shortlist,
    to_fact_command,
)
from featuregen.overlay.upload.semantic_bindings.enrich import (
    _ITEM_ALLOWED_KEYS_PROBE,
    RC_UNKNOWN_CANDIDATE_ID,
)

SVC = mint_test_service_identity(subject="service:overlay", role_claims=("overlay",),
                                 attestation="sig")
_SRC = "src"
_SCHEMA = "public"
_TABLE = "txn"
_RUN = "run_1"


# --- builders (mirror test_shortlist_validate_propose) -------------------------------------------
def _col(column: str, *, concept: str | None = None, term_type: str = "",
         semantic_type: str | None = None) -> ColumnMetadataView:
    return ColumnMetadataView(
        source=_SRC, schema=_SCHEMA, table=_TABLE, column=column,
        logical_ref=normalize_ref(_SRC, _SCHEMA, _TABLE, column),
        operational_type="text", declared_type="", term_name="", business_definition="",
        domain="", term_type=term_type, process_path="", synonyms=(), bian_path="", fibo_path="",
        semantic_type=semantic_type, logical_representation=None, concept=concept,
        drafted_definition=None, classified_domain=None, sidecar_attached=False)


def _view(columns) -> TableMetadataView:
    return TableMetadataView(
        source=_SRC, schema=_SCHEMA, table=_TABLE,
        logical_ref=normalize_ref(_SRC, _SCHEMA, _TABLE), table_definition=None, term_name=None,
        columns=tuple(columns))


def _pc(column: str, *, entity: str, eligible: bool = True) -> dict:
    return {normalize_ref(_SRC, _SCHEMA, _TABLE, column):
            PassCIdentifier(join_key_eligible=eligible, entity=entity)}


def _bounds(**over) -> SemanticBindingBounds:
    base = {"max_candidates_per_table": 40, "max_provider_calls": 8, "max_input_bytes": 16000,
            "deadline_s": 60.0}
    base.update(over)
    return SemanticBindingBounds(**base)


# --- fake LLM client -----------------------------------------------------------------------------
class ScriptedSelectLLM:
    """Reads the presented candidate_ids from the egress-safe payload and returns a selection
    response. ``responder(items) -> list[selection]`` customizes the selection; ``raw_output`` forces
    a body (e.g. a schema-violating one); ``provider_status`` drives the failure path. Records every
    request so a test can assert dispatch happened (or, on a fail-closed bound, did NOT)."""

    def __init__(self, responder=None, *, provider_status=PROVIDER_OK, raw_output=None):
        self.requests: list = []
        self._responder = responder
        self._status = provider_status
        self._raw = raw_output

    def call(self, request) -> LLMResult:
        self.requests.append(request)
        items = request.inputs["catalog_metadata"]["candidates"]
        if self._raw is not None:
            output = self._raw
        elif self._responder is not None:
            output = {"selections": self._responder(items)}
        else:  # default: select the first candidate, keep it strong
            output = {"selections": ([{"candidate_id": items[0]["candidate_id"],
                                       "disposition": "strong", "confidence": 0.9,
                                       "rationale": "ok"}] if items else [])}
        return LLMResult(output=output, self_reported_scores={}, call_ref="", status=self._status)

    @property
    def calls(self) -> int:
        return len(self.requests)


def _rows(conn, set_id):
    return conn.execute(
        "SELECT subject_graph_ref, target_graph_ref, binding_kind, disposition, proposed_value, "
        "evidence_json, reason_codes, llm_call_ref FROM semantic_binding_candidate "
        "WHERE candidate_set_id = %s ORDER BY subject_graph_ref, binding_kind", (set_id,)).fetchall()


# ==================================================================================================
# 1) SELECT-only — a known id is applied; an UNKNOWN id is DROPPED with a durable reason code
# ==================================================================================================
def test_selects_known_ids_and_drops_unknown(conn) -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code"),
                  _col("cust_id")])
    pc = _pc("cust_id", entity="customer")
    cands = shortlist(view, None, pc)
    assert len(cands) == 2                            # currency (amt→ccy) + entity (cust_id)

    def responder(items):
        currency = next(i for i in items if i["binding_kind"] == "currency_binding")
        return [
            {"candidate_id": currency["candidate_id"], "disposition": "weak", "confidence": 0.8,
             "rationale": "ambiguous on reflection"},
            {"candidate_id": "sbc_ghost_invented", "disposition": "strong", "confidence": 1.0},
        ]

    client = ScriptedSelectLLM(responder)
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   pass_c=pc, actor=SVC)

    assert client.calls == 1
    assert res.completion_status == "complete" and res.presented == 2 and res.selected == 1
    # the invented id is dropped with a durable reason code — never persisted as a candidate.
    assert ("sbc_ghost_invented", RC_UNKNOWN_CANDIDATE_ID) in res.dropped_unknown
    rows = _rows(conn, res.candidate_set_id)
    assert len(rows) == 2                             # only the two REAL candidates persist
    by_kind = {r[2]: r for r in rows}
    # currency: the model's downgrade applied; confidence stored as EVIDENCE.
    cur = by_kind["currency_binding"]
    assert cur[3] == "weak"
    assert cur[5]["llm"]["selected"] is True and cur[5]["llm"]["confidence"] == 0.8
    # entity: not selected → keeps its deterministic strong, marked not-selected.
    ent = by_kind["entity_assignment"]
    assert ent[3] == "strong" and ent[5]["llm"]["selected"] is False
    # no persisted row carries the invented id anywhere.
    assert all("ghost" not in json.dumps(r[5]) for r in rows)


# ==================================================================================================
# 2) NO unsafe egress — the model payload has no raw sample values / no raw FQN; a free-text field
#    that fails the egress policy fails CLOSED without dispatch.
# ==================================================================================================
def test_payload_is_metadata_only_no_fqn_no_samples(conn) -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    client = ScriptedSelectLLM()
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   actor=SVC)
    assert res.completion_status == "complete" and client.calls == 1
    catalog = client.requests[0].inputs["catalog_metadata"]
    blob = json.dumps(catalog)
    # NO raw FQN / logical_ref (source-scoped `::` form) and NO schema-qualified column FQN.
    assert "::" not in blob
    assert normalize_ref(_SRC, _SCHEMA, _TABLE, "amt") not in blob
    assert "public.txn.amt" not in blob and f"{_SCHEMA}.{_TABLE}" not in blob
    # only allowlisted structural keys per candidate — nothing sample-shaped.
    for item in catalog["candidates"]:
        assert set(item) <= _ITEM_ALLOWED_KEYS_PROBE
    assert catalog["table"] == _TABLE                # bare table NAME, never an FQN


def test_free_text_failing_egress_fails_closed_without_dispatch(conn) -> None:
    # a curated concept carrying a PII marker must never egress — the egress guard fails CLOSED.
    view = _view([_col("amt", concept="leak@evil.com"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    assert len(cands) == 1
    client = ScriptedSelectLLM()
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   actor=SVC)
    assert res.completion_status == "failed"
    assert client.calls == 0                          # NO dispatch — blocked before egress
    # a failed set is persisted with ZERO candidate rows (nothing egressed, nothing stored).
    assert conn.execute("SELECT count(*) FROM semantic_binding_candidate WHERE candidate_set_id = %s",
                        (res.candidate_set_id,)).fetchone()[0] == 0


# ==================================================================================================
# 3) BOUNDS — cap → partial (truthful counts); byte/call/deadline → failed, no dispatch
# ==================================================================================================
def test_candidate_cap_yields_partial_truthful_counts(conn) -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code"),
                  _col("cust_id")])
    pc = _pc("cust_id", entity="customer")
    cands = shortlist(view, None, pc)
    assert len(cands) == 2
    client = ScriptedSelectLLM()
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   pass_c=pc, actor=SVC, bounds=_bounds(max_candidates_per_table=1))
    assert res.completion_status == "partial" and res.presented == 1
    assert res.persisted == 2                         # nothing dropped: capped + over-cap both persist
    assert client.calls == 1 and "candidate_cap" in (res.reason or "")


def test_byte_cap_fails_closed_without_dispatch(conn) -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    client = ScriptedSelectLLM()
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   actor=SVC, bounds=_bounds(max_input_bytes=5))
    assert res.completion_status == "failed" and client.calls == 0
    assert "input_bytes_exceeded" in (res.reason or "")


def test_call_budget_and_deadline_fail_closed_without_dispatch(conn) -> None:
    from datetime import UTC, datetime, timedelta
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)

    c1 = ScriptedSelectLLM()
    r1 = enrich_semantic_bindings(conn, c1, table_view=view, candidates=cands, catalog_source=_SRC,
                                  ingestion_run_id=_RUN, attempt_no=1, actor=SVC, calls_remaining=0)
    assert r1.completion_status == "failed" and c1.calls == 0
    assert "call_budget" in (r1.reason or "")

    c2 = ScriptedSelectLLM()
    past = datetime.now(UTC) - timedelta(seconds=1)
    r2 = enrich_semantic_bindings(conn, c2, table_view=view, candidates=cands, catalog_source=_SRC,
                                  ingestion_run_id=_RUN, attempt_no=2, actor=SVC, deadline=past)
    assert r2.completion_status == "failed" and c2.calls == 0
    assert "deadline" in (r2.reason or "")


# ==================================================================================================
# 4) FAILURE ISOLATION — a provider error → failed set; Pass B state is UNAFFECTED
# ==================================================================================================
def test_provider_failure_is_isolated_pass_b_unaffected(conn) -> None:
    # a Pass B grain surrogate on the graph — the operational state D3 must never touch.
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "is_grain, is_as_of) VALUES ('src', 'public.txn.txn_id', 'column', 'txn', 'txn_id', "
        "true, false)")
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    client = ScriptedSelectLLM(provider_status=PROVIDER_NON_RETRYABLE)   # provider fails
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   actor=SVC)
    assert res.completion_status == "failed"          # the semantic set failed — did NOT raise
    # Pass B grain surrogate is intact (separate failure domain).
    grain = conn.execute(
        "SELECT is_grain FROM graph_node WHERE object_ref = 'public.txn.txn_id'").fetchone()
    assert grain is not None and grain[0] is True
    # the failed set carries NO candidate rows (nothing from the unusable provider response).
    assert conn.execute("SELECT count(*) FROM semantic_binding_candidate WHERE candidate_set_id = %s",
                        (res.candidate_set_id,)).fetchone()[0] == 0


def test_schema_failure_is_isolated(conn) -> None:
    # a body that violates the selection schema → repair-exhausted → failed set, nothing persisted.
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    client = ScriptedSelectLLM(raw_output={"unexpected": "shape"})    # fails reg.validate
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   actor=SVC)
    assert res.completion_status == "failed"
    assert conn.execute("SELECT count(*) FROM semantic_binding_candidate WHERE candidate_set_id = %s",
                        (res.candidate_set_id,)).fetchone()[0] == 0


# ==================================================================================================
# 5) NO PERSIST before a committed llm_call_ref (the ordering invariant)
# ==================================================================================================
def test_candidates_persist_only_after_the_llm_call_committed(conn) -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    client = ScriptedSelectLLM()
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   actor=SVC)
    assert res.completion_status == "complete" and res.llm_call_ref is not None
    # the returned ref points at a COMMITTED immutable llm_call record for THIS task — the candidate
    # rows below could only be written AFTER that outcome record existed (code ordering).
    task = conn.execute("SELECT task FROM llm_call WHERE llm_call_ref = %s",
                        (res.llm_call_ref,)).fetchone()
    assert task is not None and task[0] == "overlay.semantic_bindings"
    rows = _rows(conn, res.candidate_set_id)
    assert rows                                           # candidates exist (persisted after the call)
    # no durable dispatch store here (no FEATUREGEN_DSN) → the FK dispatch link is honestly NULL; the
    # non-null linkage is proved in test_dispatch_ref_linked_when_dsn_configured.
    assert all(r[7] is None for r in rows)


def test_failed_stage_persists_no_candidate_row(conn) -> None:
    # egress block → no llm_call, no candidate: the ordering holds trivially (nothing before a ref).
    view = _view([_col("amt", concept="leak@evil.com"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    client = ScriptedSelectLLM()
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   actor=SVC)
    assert res.completion_status == "failed" and res.llm_call_ref is None
    assert conn.execute("SELECT count(*) FROM semantic_binding_candidate WHERE candidate_set_id = %s",
                        (res.candidate_set_id,)).fetchone()[0] == 0


# ==================================================================================================
# 6) CONFIDENCE is inference evidence only — never promotion authority, never on a governed fact
# ==================================================================================================
def test_confidence_is_evidence_never_on_the_fact_command(conn, catalog) -> None:
    view = _view([_col("cust_id")])
    pc = _pc("cust_id", entity="customer")
    (ent,) = shortlist(view, None, pc)

    def responder(items):
        return [{"candidate_id": items[0]["candidate_id"], "disposition": "strong",
                 "confidence": 0.99, "rationale": "high confidence"}]

    client = ScriptedSelectLLM(responder)
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=[ent],
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   pass_c=pc, actor=SVC)
    (row,) = _rows(conn, res.candidate_set_id)
    disp, proposed_value, evidence = row[3], row[4], row[5]
    assert disp == "strong"
    # confidence is EVIDENCE only — never in the proposed_value that becomes the governed fact.
    assert proposed_value == {"entity_id": "customer"} and "confidence" not in proposed_value
    assert evidence["llm"]["confidence"] == 0.99
    # the fact command D2/E1 would build carries NO confidence — the invariant, end to end.
    cmd = to_fact_command(ent, actor=SVC, idempotency_key="k")
    assert cmd.args["proposed_value"] == {"entity_id": "customer"}
    assert "confidence" not in cmd.args["proposed_value"]


# ==================================================================================================
# C5 pre-dispatch attribution + the FK dispatch linkage (armed durable DSN, own-connection commits)
# ==================================================================================================
_D3_RUN = "ingrun_d3_test"


@pytest.fixture
def durable_dsn(monkeypatch, _dsn):
    """Point FEATUREGEN_DSN at the test cluster so the own-connection dispatch / llm_call / link
    writes really commit (mirror of the C5-T4 fixture), and create the ingestion_run the audit rows
    FK-reference. Cleanup removes everything committed OUTSIDE the rolled-back request tx."""
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
            "started_at, heartbeat_at) VALUES (%s, 'upload', 'src', 'd3-test', 'in_progress', "
            "now(), now()) ON CONFLICT (id) DO NOTHING", (_D3_RUN,))
    yield _D3_RUN
    with psycopg.connect(_dsn, autocommit=True) as c:
        c.execute("DELETE FROM llm_call_dispatch WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s)", (_D3_RUN,))
        c.execute("DELETE FROM ingestion_run_llm_call WHERE ingestion_run_id = %s", (_D3_RUN,))
        for tbl, trig in (("llm_dispatch_subject", "llm_dispatch_subject_no_mutation"),
                          ("llm_dispatch_outcome", "llm_dispatch_outcome_no_mutation"),
                          ("llm_dispatch", "llm_dispatch_no_mutation")):
            c.execute(f"ALTER TABLE {tbl} DISABLE TRIGGER {trig}")
        c.execute("DELETE FROM llm_dispatch_outcome WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s)", (_D3_RUN,))
        c.execute("DELETE FROM llm_dispatch_subject WHERE dispatch_ref IN "
                  "(SELECT dispatch_ref FROM llm_dispatch WHERE ingestion_run_id = %s)", (_D3_RUN,))
        c.execute("DELETE FROM llm_dispatch WHERE ingestion_run_id = %s", (_D3_RUN,))
        for tbl, trig in (("llm_dispatch", "llm_dispatch_no_mutation"),
                          ("llm_dispatch_subject", "llm_dispatch_subject_no_mutation"),
                          ("llm_dispatch_outcome", "llm_dispatch_outcome_no_mutation")):
            c.execute(f"ALTER TABLE {tbl} ENABLE TRIGGER {trig}")
        c.execute("ALTER TABLE llm_call DISABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM llm_call WHERE task = 'overlay.semantic_bindings'")
        c.execute("ALTER TABLE llm_call ENABLE TRIGGER llm_call_no_mutation")
        c.execute("DELETE FROM ingestion_run WHERE id = %s", (_D3_RUN,))


def test_dispatch_ref_linked_when_dsn_configured(conn, durable_dsn, _dsn) -> None:
    """With a durable dispatch store: C5 records a PRE-dispatch llm_dispatch row (attribution) BEFORE
    egress, and the persisted candidate rows FK-link to that dispatch_ref (the 1014 llm_call_ref
    column). Proves the C5 wiring + the dispatch linkage the no-DSN tests leave NULL."""
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code"),
                  _col("cust_id")])
    pc = _pc("cust_id", entity="customer")
    cands = shortlist(view, None, pc)
    client = ScriptedSelectLLM()
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=durable_dsn, attempt_no=1,
                                   pass_c=pc, actor=SVC)
    assert res.completion_status == "complete"
    # C5 pre-dispatch attribution: a committed llm_dispatch header for THIS run + task, with the
    # subject columns attributed (never the model payload — the audit trail).
    disp = conn.execute(
        "SELECT dispatch_ref, task FROM llm_dispatch WHERE ingestion_run_id = %s", (durable_dsn,)
    ).fetchone()
    assert disp is not None and disp[1] == "overlay.semantic_bindings"
    dispatch_ref = disp[0]
    subjects = {r[0] for r in conn.execute(
        "SELECT object_ref FROM llm_dispatch_subject WHERE dispatch_ref = %s", (dispatch_ref,))}
    assert "public.txn.amt" in subjects                  # the measure subject is attributed
    # every persisted candidate row FK-links to that pre-dispatch authorization.
    rows = _rows(conn, res.candidate_set_id)
    assert rows and all(r[7] == dispatch_ref for r in rows)
    # release the uncommitted candidate rows BEFORE the fixture deletes the parent llm_dispatch rows
    # (their FK would otherwise block the own-connection cleanup DELETE).
    conn.rollback()


def test_confidence_can_never_upgrade_a_weak_candidate(conn) -> None:
    # ambiguous currency → two WEAK candidates. A confident model 'strong' can never upgrade them.
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code"),
                  _col("settle_ccy", concept="base_currency")])
    cands = shortlist(view)
    assert len(cands) == 2 and all(c.disposition == "weak" for c in cands)

    def responder(items):
        return [{"candidate_id": i["candidate_id"], "disposition": "strong", "confidence": 1.0}
                for i in items]

    client = ScriptedSelectLLM(responder)
    res = enrich_semantic_bindings(conn, client, table_view=view, candidates=cands,
                                   catalog_source=_SRC, ingestion_run_id=_RUN, attempt_no=1,
                                   actor=SVC)
    rows = _rows(conn, res.candidate_set_id)
    assert rows and all(r[3] == "weak" for r in rows)     # confidence is NOT promotion authority
    # but the model's high confidence IS recorded as evidence on each selected row.
    assert all(r[5]["llm"]["confidence"] == 1.0 for r in rows)
