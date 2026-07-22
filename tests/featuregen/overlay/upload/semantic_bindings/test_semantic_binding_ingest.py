"""D4 — semantic-binding candidate + proposal stages wired into ``ingest_upload``.

Behind ``OVERLAY_SEMANTIC_BINDING_CANDIDATES`` / ``OVERLAY_SEMANTIC_BINDING_PROPOSALS`` (both default
OFF). Properties under test (mirroring the Pass C ingest keystone):

1. FLAG OFF — byte-identical: both flags off is today's behaviour PLUS a truthful ``disabled`` stage
   entry for each new stage; no candidate rows / sets / current projection; the four new IngestResult
   counts are all 0.
2. CANDIDATES ON — an upload persists an immutable candidate set + a ``current`` CAS projection, with
   truthful counts; proposals off -> no DRAFT facts.
3. PROPOSALS ON — a strong candidate becomes an E1 governed DRAFT fact + link (never VERIFIED);
   proposals ON without candidates is the INVALID combo -> ``not_applicable`` no-op, nothing written.
4. FAIL-SOFT — a DB abort inside the candidate stage is contained (savepoint): the upload still
   ingests, Pass A facts + graph hold, the stage reports ``failed``.
5. RE-INGEST INVALIDATION (disabled) — a metadata change on a re-ingest with the producer DISABLED
   still flips the stale current set to ``unverifiable`` (NO LLM call), keeping the old set as
   history. Disabling the producer must NOT freeze a stale current set.
6. GATE — the semantic stages leave feature + contract row counts UNCHANGED and call NO
   feature-assist / compiler entrypoint (asserted via spy).
7. BOUNDS — a D3 bound overflow / provider fault (a 126-col-scale table) yields a ``partial`` semantic
   stage WITHOUT changing upload acceptance; the deterministic set stays ``current``.
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.facts import CURRENCY_BINDING
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import (
    ingest_upload,
    semantic_binding_candidates_enabled,
)
from featuregen.overlay.upload.stage_report import StageRecorder

_NOW = datetime(2026, 7, 20, tzinfo=UTC)
_CANDS = "OVERLAY_SEMANTIC_BINDING_CANDIDATES"
_PROPS = "OVERLAY_SEMANTIC_BINDING_PROPOSALS"
_SOURCE = "bank"


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _currency_rows() -> list[CanonicalRow]:
    """A measure (``amount``) + EXACTLY ONE currency column (``currency``) in one table -> the D2
    shortlist enumerates a single STRONG ``currency_binding`` candidate (amount -> currency)."""
    return [
        CanonicalRow(_SOURCE, "transactions", "txn_id", "integer", is_grain=True),
        CanonicalRow(_SOURCE, "transactions", "amount", "numeric"),
        CanonicalRow(_SOURCE, "transactions", "currency", "text"),
    ]


def _wide_transactions() -> list[CanonicalRow]:
    """A 5-column ``transactions`` table (one STRONG currency candidate). Wide enough that dropping a
    small 2-column sibling table keeps the re-upload's object overlap above the large-change brake's
    60% floor (5+1 of 5+1+2+1 = 67%), so the re-ingest invalidation actually runs (not held)."""
    return [
        CanonicalRow(_SOURCE, "transactions", "txn_id", "integer", is_grain=True),
        CanonicalRow(_SOURCE, "transactions", "amount", "numeric"),
        CanonicalRow(_SOURCE, "transactions", "currency", "text"),
        CanonicalRow(_SOURCE, "transactions", "memo", "text"),
        CanonicalRow(_SOURCE, "transactions", "note", "text"),
    ]


def _wide_two_table_rows() -> list[CanonicalRow]:
    """``transactions`` (wide) + a small ``accounts`` sibling -> two current candidate sets."""
    return [
        *_wide_transactions(),
        CanonicalRow(_SOURCE, "accounts", "acct_id", "integer", is_grain=True),
        CanonicalRow(_SOURCE, "accounts", "ccy", "text"),
    ]


def _amount_currency_key() -> str:
    return fact_key(
        CatalogObjectRef(catalog_source=_SOURCE, object_kind="column", schema="public",
                         table="transactions", column="amount"),
        CURRENCY_BINDING)


def _count(conn, table: str, source: str = _SOURCE) -> int:
    return conn.execute(
        f"SELECT count(*) FROM {table} WHERE catalog_source = %s", (source,)).fetchone()[0]


def _current(conn, source: str = _SOURCE):
    return conn.execute(
        "SELECT table_graph_ref, candidate_set_id, status "
        "FROM current_semantic_binding_candidate_set WHERE catalog_source = %s "
        "ORDER BY table_graph_ref", (source,)).fetchall()


def _proposals(conn, source: str = _SOURCE) -> int:
    return conn.execute(
        "SELECT count(*) FROM semantic_binding_candidate_proposal p "
        "JOIN semantic_binding_candidate c ON c.candidate_id = p.candidate_id "
        "WHERE c.catalog_source = %s", (source,)).fetchone()[0]


def _states(rec: StageRecorder) -> dict[str, tuple[str, str | None]]:
    return {r.stage: (r.state, r.reason_code) for r in rec.reports}


# ── 1. FLAG OFF — byte-identical + a truthful `disabled` stage entry ──────────────────────────────

def test_flag_off_is_byte_identical(db, monkeypatch):
    monkeypatch.delenv(_CANDS, raising=False)
    monkeypatch.delenv(_PROPS, raising=False)
    assert semantic_binding_candidates_enabled() is False

    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW, stage_recorder=rec)
    assert res.status == "ingested"

    # The four new counts are all 0 (byte-identical semantic-binding surface).
    assert (res.semantic_binding_candidates, res.semantic_binding_proposed,
            res.semantic_binding_abstained, res.semantic_binding_failed) == (0, 0, 0, 0)
    # NO candidate rows / sets / current projection written.
    assert _count(db, "semantic_binding_candidate_set") == 0
    assert _count(db, "semantic_binding_candidate") == 0
    assert _current(db) == []
    # A truthful `disabled` stage entry for BOTH new stages (never a silent skip).
    states = _states(rec)
    assert states["semantic_binding_candidates"] == ("disabled", None)
    assert states["semantic_binding_proposals"] == ("disabled", None)


# ── 2. CANDIDATES ON — a set + current projection + truthful counts; proposals off = no DRAFT ──────

def test_candidates_flag_on_writes_set_and_current(db, monkeypatch):
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.delenv(_PROPS, raising=False)

    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW, stage_recorder=rec)
    assert res.status == "ingested"
    assert res.semantic_binding_candidates == 1                 # one strong currency candidate
    assert _count(db, "semantic_binding_candidate") == 1

    current = _current(db)
    assert len(current) == 1
    assert current[0][0] == "public.transactions"
    assert current[0][1] is not None and current[0][2] == "current"   # a set is current
    # Proposals flag OFF -> no DRAFT fact.
    assert res.semantic_binding_proposed == 0 and _proposals(db) == 0
    assert load_fact(db, _amount_currency_key()) == []
    states = _states(rec)
    assert states["semantic_binding_candidates"][0] == "succeeded"
    assert states["semantic_binding_proposals"] == ("disabled", None)


# ── 3. PROPOSALS ON — a strong candidate -> an E1 DRAFT fact + link (never VERIFIED) ───────────────

def test_proposals_flag_on_creates_draft_facts(db, monkeypatch):
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv(_PROPS, "1")

    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"
    assert res.semantic_binding_proposed == 1
    assert _proposals(db) == 1                                  # the candidate -> proposal link exists

    events = load_fact(db, _amount_currency_key())
    assert any(e.type == "OVERLAY_FACT_PROPOSED" for e in events)   # routed as a DRAFT proposal
    assert fold_overlay_state(events).status == "DRAFT"            # NEVER VERIFIED without humans


def test_proposals_without_candidates_is_invalid_noop(db, monkeypatch):
    monkeypatch.delenv(_CANDS, raising=False)
    monkeypatch.setenv(_PROPS, "1")                             # proposals ON, candidates OFF

    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW, stage_recorder=rec)
    assert res.status == "ingested"
    assert res.semantic_binding_proposed == 0
    # Nothing written — a proposal has no persisted candidate to link.
    assert _count(db, "semantic_binding_candidate") == 0 and _proposals(db) == 0
    assert load_fact(db, _amount_currency_key()) == []
    states = _states(rec)
    assert states["semantic_binding_candidates"] == ("disabled", None)
    assert states["semantic_binding_proposals"] == ("not_applicable", "requires_candidates")


# ── 4. FAIL-SOFT — a DB abort in the candidate stage is contained; Pass A facts + graph hold ───────

def test_candidate_stage_db_abort_is_contained(db, monkeypatch):
    monkeypatch.setenv(_CANDS, "1")

    import featuregen.overlay.upload.ingest as ingest_mod

    def _db_abort(conn, *a, **kw):
        # A REAL DB fault (undefined table) aborts the tx; the stage savepoint must contain it.
        conn.execute("SELECT boom FROM nonexistent_semantic_binding_table").fetchall()

    monkeypatch.setattr(ingest_mod, "_run_semantic_binding_candidate_stage", _db_abort)
    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW, stage_recorder=rec)

    assert res.status == "ingested"                             # never raises out of ingest
    assert res.asserted >= 1                                    # the Pass A grain fact asserted
    assert res.semantic_binding_failed == 1
    assert _count(db, "semantic_binding_candidate") == 0        # the aborted stage wrote nothing
    n = db.execute("SELECT count(*) FROM graph_node WHERE catalog_source = %s AND kind = 'column'",
                   (_SOURCE,)).fetchone()[0]
    assert n == 3                                               # graph intact
    assert _states(rec)["semantic_binding_candidates"] == ("failed", "exception")


# ── 5. RE-INGEST INVALIDATION (disabled) — disabling the producer can't freeze a stale current set ─

def test_reingest_invalidation_when_disabled(db, monkeypatch):
    # (1) producer ON -> build a current candidate set.
    monkeypatch.setenv(_CANDS, "1")
    res1 = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW)
    assert res1.status == "ingested" and res1.semantic_binding_candidates == 1
    current = _current(db)
    assert len(current) == 1 and current[0][1] is not None and current[0][2] == "current"
    sets_before = _count(db, "semantic_binding_candidate_set")

    # (2) DISABLE the producer, then re-ingest with CHANGED table metadata (a new column changes the
    #     fingerprint). A spy proves NO LLM/D3 call happens on the disabled invalidation path.
    monkeypatch.delenv(_CANDS, raising=False)
    monkeypatch.delenv(_PROPS, raising=False)
    import featuregen.overlay.upload.semantic_bindings.enrich as enrich_mod
    calls: list[int] = []
    monkeypatch.setattr(enrich_mod, "enrich_semantic_bindings",
                        lambda *a, **kw: calls.append(1))
    changed = [*_currency_rows(), CanonicalRow(_SOURCE, "transactions", "memo", "text")]
    res2 = ingest_upload(db, _SOURCE, changed, actor=_actor(), now=_NOW)

    assert res2.status == "ingested"
    assert res2.semantic_binding_candidates == 0               # producer disabled -> no new candidates
    assert calls == []                                          # NO LLM call
    # The stale current set is invalidated: status unverifiable, candidate_set_id NULL.
    current2 = _current(db)
    assert len(current2) == 1 and current2[0][1] is None and current2[0][2] == "unverifiable"
    # The old immutable set is KEPT as history (the disabled run wrote no new set).
    assert _count(db, "semantic_binding_candidate_set") == sets_before


# ── 5b. I-B — a table DROPPED from a re-upload has its current set flipped to `unverifiable` ───────

def test_reingest_dropped_table_current_set_becomes_unverifiable(db, monkeypatch):
    monkeypatch.setenv(_CANDS, "1")
    res1 = ingest_upload(db, _SOURCE, _wide_two_table_rows(), actor=_actor(), now=_NOW)
    assert res1.status == "ingested"
    current1 = {r[0]: (r[1], r[2]) for r in _current(db)}
    assert current1["public.transactions"][1] == "current"
    assert current1["public.accounts"][1] == "current"            # both tables current

    # re-upload WITHOUT the accounts table -> it is dropped from the source (overlap stays > 60%).
    res2 = ingest_upload(db, _SOURCE, _wide_transactions(), actor=_actor(), now=_NOW)
    assert res2.status == "ingested"
    current2 = {r[0]: (r[1], r[2]) for r in _current(db)}
    assert current2["public.transactions"][1] == "current"        # still present -> re-projected
    # the DROPPED table's current set is invalidated (candidate_set_id NULL) — the immutable set stays.
    assert current2["public.accounts"] == (None, "unverifiable")


# ── 5c. I-C — a candidate LEAVING the current set stales its linked DRAFT proposal (fact untouched) ─

def test_reingest_stales_orphaned_draft_proposal(db, monkeypatch):
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv(_PROPS, "1")
    res1 = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW)
    assert res1.semantic_binding_proposed == 1 and _proposals(db) == 1
    assert fold_overlay_state(load_fact(db, _amount_currency_key())).status == "DRAFT"

    # re-upload WITHOUT the currency column -> the amount->currency candidate can no longer be
    # enumerated, so it LEAVES the current set; `stale_orphaned_proposals` (wired into the candidate
    # stage) retires its now-orphaned DRAFT link. The new set proposes nothing (no currency column).
    no_ccy = [CanonicalRow(_SOURCE, "transactions", "txn_id", "integer", is_grain=True),
              CanonicalRow(_SOURCE, "transactions", "amount", "numeric")]
    res2 = ingest_upload(db, _SOURCE, no_ccy, actor=_actor(), now=_NOW)
    assert res2.status == "ingested"
    assert _proposals(db) == 0                                     # the orphaned DRAFT link retired
    # the DRAFT fact itself is UNTOUCHED — only a VERIFIED fact's own governed deps invalidate it, and
    # a DRAFT is never served (VERIFIED-survival is proven in test_store_projection).
    assert fold_overlay_state(load_fact(db, _amount_currency_key())).status == "DRAFT"


# ── 6. GATE — feature + contract row counts unchanged + NO compiler entrypoint called ─────────────

def test_gate_no_feature_or_contract_rows_and_no_compiler(db, monkeypatch):
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv(_PROPS, "1")

    import featuregen.overlay.upload.planner.declarations as decl
    compiler_calls: list[str] = []
    for name in ("compile_contract", "compile_aggregation", "compile_temporal"):
        monkeypatch.setattr(decl, name,
                            lambda *a, _n=name, **kw: compiler_calls.append(_n))

    def _rows(table: str) -> int:
        return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    before = (_rows("feature"), _rows("contract"))
    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"
    assert res.semantic_binding_proposed == 1                  # the semantic stages DID run
    assert (_rows("feature"), _rows("contract")) == before     # feature/contract rows UNCHANGED
    assert compiler_calls == []                                 # no feature-assist / compiler call


# ── 7. BOUNDS — a D3 bound overflow yields a `partial` stage WITHOUT changing acceptance ───────────

def test_d3_bound_overflow_is_partial_upload_accepted(db, monkeypatch):
    import featuregen.overlay.upload.semantic_bindings.enrich as enrich_mod
    from featuregen.intake.llm import FakeLLM
    from featuregen.overlay.upload.semantic_bindings.enrich import EnrichResult

    monkeypatch.setenv(_CANDS, "1")

    def _bound_failed(*a, **kw):
        # A bound overflow (byte/call/deadline) / provider fault -> a `failed` set, NEVER a crash.
        return EnrichResult(completion_status="failed", candidate_set_id=None, llm_call_ref=None,
                            presented=0, selected=0, persisted=0, reason="input_bytes_exceeded")

    monkeypatch.setattr(enrich_mod, "enrich_semantic_bindings", _bound_failed)
    rec = StageRecorder()
    # A non-None client makes the candidate stage run its D3 sub-call (here the stubbed bound failure);
    # Pass A degrades fail-soft on the unscripted FakeLLM, which does not affect the semantic stages.
    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW,
                        client=FakeLLM(script={}), stage_recorder=rec)

    assert res.status == "ingested"                            # acceptance UNCHANGED
    assert res.semantic_binding_candidates == 1                # the deterministic set still persisted
    assert res.semantic_binding_failed == 1                    # the D3 stage hit its bound
    assert _current(db)[0][2] == "current"                     # deterministic set stays current
    assert _states(rec)["semantic_binding_candidates"] == ("partial", "llm_bound")


# ── 7b. M-2 — a D3 `partial` (candidate-cap overflow) marks the stage `partial`, NOT a failure ─────

def test_d3_partial_marks_stage_partial_not_failed(db, monkeypatch):
    import featuregen.overlay.upload.semantic_bindings.enrich as enrich_mod
    from featuregen.intake.llm import FakeLLM
    from featuregen.overlay.upload.semantic_bindings.enrich import EnrichResult

    monkeypatch.setenv(_CANDS, "1")

    def _partial(*a, **kw):
        # A candidate-cap overflow -> a `partial` D3 set (the capped subset IS ranked): the stage is
        # `partial` (truthful), but a `partial` is NOT a failure — it must not inflate `failed`.
        return EnrichResult(completion_status="partial", candidate_set_id="cs", llm_call_ref="ref",
                            presented=1, selected=1, persisted=2, reason="candidate_cap_exceeded")

    monkeypatch.setattr(enrich_mod, "enrich_semantic_bindings", _partial)
    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW,
                        client=FakeLLM(script={}), stage_recorder=rec)

    assert res.status == "ingested"
    assert res.semantic_binding_candidates == 1
    assert res.semantic_binding_failed == 0                    # a `partial` is NOT a failure (M-2)
    assert _states(rec)["semantic_binding_candidates"] == ("partial", "llm_bound")


# ── 8. I-D — the RUN-LEVEL LLM provider-call budget bounds provider calls; the rest ABSTAIN ────────

class _CountingSelectLLM:
    """Records every request; answers a D3 SELECT with a valid selection and lets Pass A degrade on an
    off-schema body. ``sembind_calls`` counts ONLY the semantic-binding provider dispatches."""

    def __init__(self):
        self.requests: list = []

    def call(self, request):
        from featuregen.intake.llm import PROVIDER_OK, LLMResult
        self.requests.append(request)
        if request.task == "overlay.semantic_bindings":
            items = request.inputs["catalog_metadata"]["candidates"]
            output = {"selections": ([{"candidate_id": items[0]["candidate_id"],
                                       "disposition": "strong", "confidence": 0.9,
                                       "rationale": "ok"}] if items else [])}
        else:
            output = {}      # Pass A: an off-schema body -> that stage degrades fail-soft (not our SUT)
        return LLMResult(output=output, self_reported_scores={}, call_ref="", status=PROVIDER_OK)

    @property
    def sembind_calls(self) -> int:
        return sum(1 for r in self.requests if r.task == "overlay.semantic_bindings")


def _one_currency_table(table: str) -> list[CanonicalRow]:
    return [CanonicalRow(_SOURCE, table, "id", "integer", is_grain=True),
            CanonicalRow(_SOURCE, table, "amount", "numeric"),
            CanonicalRow(_SOURCE, table, "currency", "text")]


def test_run_level_llm_budget_bounds_provider_calls_rest_abstain(db, monkeypatch):
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv("OVERLAY_SEMBIND_MAX_PROVIDER_CALLS", "2")   # run-level budget of 2 calls
    # FOUR tables, each with one STRONG currency candidate -> four D3 dispatches attempted.
    rows = [*_one_currency_table("transactions"), *_one_currency_table("invoices"),
            *_one_currency_table("payments"), *_one_currency_table("refunds")]
    client = _CountingSelectLLM()
    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, rows, actor=_actor(), now=_NOW, client=client, stage_recorder=rec)

    assert res.status == "ingested"                            # upload STILL accepted
    assert res.semantic_binding_candidates == 4                # the deterministic set for EVERY table
    # At most `budget` provider calls actually reached the model (was ~1-per-table before I-D).
    assert client.sembind_calls == 2
    # The two budget-refused tables ABSTAIN: their D3 set is `failed` (no dispatch) -> counted failed.
    assert res.semantic_binding_failed == 2
    assert _states(rec)["semantic_binding_candidates"] == ("partial", "llm_bound")


# ── 9. I-E — a candidate-stage PREP exception records `failed` AND the invalidation still runs ─────

def test_prep_exception_records_failed_and_still_invalidates(db, monkeypatch):
    monkeypatch.setenv(_CANDS, "1")
    # (1) producer ON -> build a current candidate set for transactions.
    res1 = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW)
    assert res1.status == "ingested" and res1.semantic_binding_candidates == 1
    assert _current(db)[0][2] == "current"

    # (2) force the metadata PREP to throw on the re-upload. The stage must record `failed` (truthful,
    #     not a vacuous succeeded/tables=0) AND the always-on re-ingest invalidation must STILL run —
    #     with no live fingerprints it fail-closes the prior current set to `unverifiable`.
    import featuregen.overlay.upload.ingest as ingest_mod

    def _prep_boom(*a, **kw):
        raise RuntimeError("semantic-binding prep blew up")

    monkeypatch.setattr(ingest_mod, "_semantic_binding_prep", _prep_boom)
    rec = StageRecorder()
    res2 = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW, stage_recorder=rec)

    assert res2.status == "ingested"                           # never aborts the upload
    assert _states(rec)["semantic_binding_candidates"] == ("failed", "prep_exception")
    assert res2.semantic_binding_failed == 1                   # truthfully counted a failure
    current2 = _current(db)                                    # the invalidation STILL ran
    assert current2 and current2[0][1] is None and current2[0][2] == "unverifiable"


# ── 10. M-3 — after a candidate-stage FAILURE the proposal stage is `skipped`, not a vacuous succeed ─

def test_proposal_stage_skipped_after_candidate_stage_failure(db, monkeypatch):
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv(_PROPS, "1")
    import featuregen.overlay.upload.ingest as ingest_mod

    def _db_abort(conn, *a, **kw):
        conn.execute("SELECT boom FROM nonexistent_semantic_binding_table").fetchall()

    monkeypatch.setattr(ingest_mod, "_run_semantic_binding_candidate_stage", _db_abort)
    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_actor(), now=_NOW, stage_recorder=rec)

    assert res.status == "ingested"
    states = _states(rec)
    assert states["semantic_binding_candidates"] == ("failed", "exception")
    # M-3: no persisted candidate to link -> the proposal stage is truthfully `not_applicable`.
    assert states["semantic_binding_proposals"] == ("not_applicable", "upstream_failed")
    assert res.semantic_binding_proposed == 0
