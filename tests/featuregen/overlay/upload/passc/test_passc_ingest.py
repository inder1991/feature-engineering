"""Task 10 — Pass C ingest wiring + governed mode, behind OVERLAY_PASS_C (default OFF).

The INTEGRATION KEYSTONE. Properties under test:

1. FLAG OFF (the sacred test): with OVERLAY_PASS_C and OVERLAY_GOVERNED_JOINS both unset, ingest is
   byte-for-byte today's behaviour — the Pass C compute path is never entered (spied), no candidate
   ledger rows, no approved_join facts, the declared 'joins' edge stays authority='operational' and
   `find_join_path` still traverses it.
2. FLAG ON — governed routing: a declared `joins_to` writes a display_only edge AND is routed into a
   governed approved_join proposal; `find_join_path` returns None pre-confirm.
3. FLAG ON — strong candidate: a concept/entity-shared column pair with ONE confirmed-grain side is
   scored strong, persisted to the ledger, and proposed (fact_key stamped back onto the row; the
   fact is DRAFT — never VERIFIED without the dual human confirm).
4. FLAG ON — weak-only: a POSSIBLE-namespace pair persists as a weak ledger diagnostic
   (bucket='weak', lifecycle='weak', fact_key NULL); nothing is proposed.
5. FAIL-SOFT: a DB abort inside the Pass C compute block degrades to a warning — the upload still
   ingests, Pass A facts + the graph hold (savepoint containment).
6. LOOP-CLOSER (Task 8 wired): a VERIFIED approved_join is re-projected onto the rebuilt graph at
   the end of a subsequent ingest — build_graph wipes every edge, so the fact stream (never the
   just-cleared ledger) is the enumeration source.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.overlay.upload.passc.conftest import _confirm_join

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.identity import fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.graph import governed_join_proposal, governed_joins_enabled
from featuregen.overlay.upload.ingest import ingest_upload, pass_c_enabled
from featuregen.overlay.upload.join_path import find_join_path
from featuregen.overlay.upload.object_ref import normalize_ref

_NOW = datetime(2026, 7, 13, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _declared_join_rows() -> list[CanonicalRow]:
    """A declared `joins_to` (transactions.acct_id -> accounts.account_id) + the target grain.
    Deliberately yields NO Pass C candidate (no shared entity/term -> AMBIGUOUS, never blocked),
    so the governed ROUTING of the declared join is observed in isolation."""
    return [
        CanonicalRow("deposits", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("deposits", "accounts", "account_id", "integer", is_grain=True),
    ]


def _rec(source: str, table: str, column: str, term: str) -> GlossaryRecord:
    return GlossaryRecord(
        logical_ref=normalize_ref(source, "public", table, column),
        term_name=term, definition=f"The {term}.", domain="Customer",
        synonyms=(), bian_path="Customer Management/Customer Reference", fibo_path="")


def _crm_rows() -> list[CanonicalRow]:
    """Two customer_id columns sharing name + entity, one side a declared grain -> strong."""
    return [
        CanonicalRow("crm", "customers", "customer_id", "integer",
                     is_grain=True, entity="Customer"),
        CanonicalRow("crm", "cases", "customer_id", "integer", entity="Customer"),
    ]


def _crm_glossary() -> GlossaryUpload:
    return GlossaryUpload(rows=[], records=[
        _rec("crm", "customers", "customer_id", "Customer Identifier"),
        _rec("crm", "cases", "customer_id", "Customer Identifier")])


def _edge_row(conn, source: str, from_ref: str):
    return conn.execute(
        "SELECT authority, approved_join_fact_key FROM graph_edge WHERE catalog_source=%s "
        "AND kind='joins' AND from_ref=%s", (source, from_ref)).fetchone()


def _ledger_rows(conn, source: str):
    return conn.execute(
        "SELECT bucket, lifecycle, fact_key FROM pass_c_candidate_evidence "
        "WHERE catalog_source=%s ORDER BY from_ref, to_ref", (source,)).fetchall()


# ── 1. The sacred flag-off byte-for-byte test ─────────────────────────────────────────────────────

def test_flag_off_ingest_is_byte_for_byte(passc_conn, monkeypatch):
    monkeypatch.delenv("OVERLAY_PASS_C", raising=False)
    monkeypatch.delenv("OVERLAY_GOVERNED_JOINS", raising=False)

    # Spy on the Pass C entry points: flag-off must never even ENTER the compute/propose path.
    entered: list[str] = []
    import featuregen.overlay.upload.passc.candidates as candidates_mod
    import featuregen.overlay.upload.passc.propose as propose_mod
    real_block = candidates_mod.block_candidates
    monkeypatch.setattr(candidates_mod, "block_candidates",
                        lambda *a, **kw: (entered.append("block"), real_block(*a, **kw))[1])
    real_propose = propose_mod.propose_join_candidates
    monkeypatch.setattr(propose_mod, "propose_join_candidates",
                        lambda *a, **kw: (entered.append("propose"), real_propose(*a, **kw))[1])

    res = ingest_upload(passc_conn, "deposits", _declared_join_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"
    assert entered == []                                        # Pass C never entered

    # Today's behaviour, byte-for-byte: operational declared edge, no fact link, no ledger rows,
    # no approved_join fact stream — and the declared join still routes feature construction.
    assert _edge_row(passc_conn, "deposits", "public.transactions.acct_id") == ("operational", None)
    assert _ledger_rows(passc_conn, "deposits") == []
    ref = governed_join_proposal(_declared_join_rows()[0])
    assert load_fact(passc_conn, fact_key(ref, "approved_join")) == []
    path = find_join_path(passc_conn, "deposits", "transactions", "accounts")
    assert path is not None and len(path) == 1


def test_pass_c_flag_widens_governed_predicate(monkeypatch):
    monkeypatch.delenv("OVERLAY_PASS_C", raising=False)
    monkeypatch.delenv("OVERLAY_GOVERNED_JOINS", raising=False)
    assert pass_c_enabled() is False and governed_joins_enabled() is False
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    assert pass_c_enabled() is True and governed_joins_enabled() is True   # Pass C implies governed
    monkeypatch.delenv("OVERLAY_PASS_C")
    monkeypatch.setenv("OVERLAY_GOVERNED_JOINS", "1")
    assert pass_c_enabled() is False and governed_joins_enabled() is True  # governed alone unchanged


# ── 2. Flag on: a declared join is display-only AND routed to a governed proposal ─────────────────

def test_flag_on_declared_join_display_only_and_routed(passc_conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    monkeypatch.delenv("OVERLAY_GOVERNED_JOINS", raising=False)

    res = ingest_upload(passc_conn, "deposits", _declared_join_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"

    row = _edge_row(passc_conn, "deposits", "public.transactions.acct_id")
    assert row is not None and row[0] == "display_only"          # raw edge is display-only
    ref = governed_join_proposal(_declared_join_rows()[0])
    events = load_fact(passc_conn, fact_key(ref, "approved_join"))
    assert any(e.type == "OVERLAY_FACT_PROPOSED" for e in events)   # routed, not stranded
    # Pre-confirm, feature construction must NOT traverse the declared join.
    assert find_join_path(passc_conn, "deposits", "transactions", "accounts") is None


# ── 3. Flag on: a strong candidate is persisted + proposed (fact_key stamped) ─────────────────────

def test_flag_on_strong_candidate_is_proposed_and_stamped(passc_conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")

    res = ingest_upload(passc_conn, "crm", _crm_rows(), actor=_actor(), now=_NOW,
                        glossary=_crm_glossary())
    assert res.status == "ingested"

    rows = _ledger_rows(passc_conn, "crm")
    assert len(rows) == 1
    bucket, _lifecycle, fk = rows[0]
    assert bucket == "strong" and fk is not None                 # proposed -> fact_key stamped
    events = load_fact(passc_conn, fk)
    assert any(e.type == "OVERLAY_FACT_PROPOSED" for e in events)
    assert fold_overlay_state(events).status == "DRAFT"          # never VERIFIED without humans
    # No edge exists between the two tables pre-confirm (nothing declared, nothing projected).
    assert find_join_path(passc_conn, "crm", "cases", "customers") is None


# ── 4. Flag on: a weak-only pair is a ledger diagnostic, never a proposal ─────────────────────────

def test_flag_on_weak_only_pair_is_ledger_diagnostic_not_proposed(passc_conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")

    # Same identifier concept ("Customer Identifier"), DIFFERENT canonical column names, no
    # synonyms/entities -> namespace POSSIBLE -> capped at weak even though one side is a grain.
    rows = [CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True),
            CanonicalRow("crm", "loans", "cust_ref", "integer")]
    glossary = GlossaryUpload(rows=[], records=[
        _rec("crm", "customers", "customer_id", "Customer Identifier"),
        _rec("crm", "loans", "cust_ref", "Customer Identifier")])

    res = ingest_upload(passc_conn, "crm", rows, actor=_actor(), now=_NOW, glossary=glossary)
    assert res.status == "ingested"

    assert _ledger_rows(passc_conn, "crm") == [("weak", "weak", None)]   # diagnostic only
    # Nothing was proposed: no approved_join gate task was ever opened.
    assert passc_conn.execute("SELECT count(*) FROM human_tasks").fetchone()[0] == 0


# ── 5. Fail-soft: a Pass C DB abort is contained; Pass A facts + graph hold ───────────────────────

def test_pass_c_db_abort_is_contained(passc_conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")

    import featuregen.overlay.upload.ingest as ingest_mod

    def _db_abort(conn, *a, **kw):
        # A REAL DB fault (undefined table) — aborts the tx; the savepoint must contain it.
        conn.execute("SELECT boom FROM nonexistent_pass_c_table").fetchall()

    monkeypatch.setattr(ingest_mod, "_pass_c_columns", _db_abort)
    res = ingest_upload(passc_conn, "crm", _crm_rows(), actor=_actor(), now=_NOW,
                        glossary=_crm_glossary())
    assert res.status == "ingested"                              # never raises out of ingest
    assert res.asserted >= 1                                     # the Pass A grain fact asserted
    n = passc_conn.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source='crm' AND kind='column'"
    ).fetchone()[0]
    assert n == 2                                                # graph intact
    assert _ledger_rows(passc_conn, "crm") == []                 # the aborted cycle wrote nothing


# ── 6. Loop-closer: a VERIFIED join is re-projected from its FACT after a graph rebuild ───────────

def test_verified_join_reprojected_after_reupload(passc_conn, monkeypatch,
                                                  human_admin_1, human_admin_2):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    rows = _declared_join_rows()

    res = ingest_upload(passc_conn, "deposits", rows, actor=_actor(), now=_NOW)
    assert res.status == "ingested"
    ref = governed_join_proposal(rows[0])
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)   # dual -> VERIFIED

    # Re-upload: build_graph wipes every edge and rewrites the declared one display_only; the
    # end-of-ingest projector must restore the governed OPERATIONAL edge from the VERIFIED fact.
    res2 = ingest_upload(passc_conn, "deposits", rows, actor=_actor(), now=_NOW)
    assert res2.status == "ingested"
    row = _edge_row(passc_conn, "deposits", "public.transactions.acct_id")
    assert row is not None
    assert row[0] == "operational" and row[1] == fact_key(ref, "approved_join")
    path = find_join_path(passc_conn, "deposits", "transactions", "accounts")
    assert path is not None and len(path) == 1                   # traversable again post-confirm


# ── 7. FTR adapter (A1): the sidecar's term_type reaches ColMeta, so Measures can't anchor joins ──

class _NoGraphConn:
    """Stub for `_pass_c_columns`' only conn use — `entity_of` runs one SELECT and needs
    `fetchone() -> None` (no graph node); everything else in the assembly is pure."""

    def execute(self, *_args):
        return self

    def fetchone(self):
        return None


def test_pass_c_columns_thread_glossary_term_type():
    from featuregen.overlay.upload.ingest import _pass_c_columns
    from featuregen.overlay.upload.passc.identifiers import is_join_key_eligible

    rows = [CanonicalRow("ftr", "positions", "settlement_id", "unknown"),
            CanonicalRow("ftr", "positions", "position_id", "unknown", is_grain=True)]
    glossary = GlossaryUpload(rows=rows, records=[
        GlossaryRecord(logical_ref=normalize_ref("ftr", "risk", "positions", "settlement_id"),
                       term_name="Settlement Total", definition="The settlement total.",
                       term_type="measure"),
        GlossaryRecord(logical_ref=normalize_ref("ftr", "risk", "positions", "position_id"),
                       term_name="Position Identifier", definition="The position id.",
                       term_type="dimension")])

    cols = {c.column: c for c in _pass_c_columns(
        _NoGraphConn(), "ftr", rows, concepts=None, glossary=glossary)}

    # settlement_id is id-like by suffix and its term trips no negative token — ONLY the threaded
    # term_type keeps this glossary-declared Measure out of the join-key candidate pool.
    assert cols["settlement_id"].term_type == "measure"
    assert is_join_key_eligible(cols["settlement_id"]) is False
    assert cols["position_id"].term_type == "dimension"
    assert is_join_key_eligible(cols["position_id"]) is True
