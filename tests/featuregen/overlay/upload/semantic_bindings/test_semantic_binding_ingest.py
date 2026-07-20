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
