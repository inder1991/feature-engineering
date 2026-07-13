"""Task 6 — fingerprint + dedupe lifecycle.

`decide_action` adjudicates the UNORDERED **COLUMN**-ref pair, NOT the table pair — two joins on
DIFFERENT columns between the same tables are legitimate (second-join case → PROPOSE, never
CONFLICT). A same-`fact_key` active fact dedupes (SKIP_ACTIVE); a DIFFERENT-`fact_key` ACTIVE fact
for the SAME column pair (different direction/cardinality) is a CONFLICT; a terminal same-key
whose prior ledger bucket/namespace materially changed re-proposes (REPROPOSE).

The VERIFIED case drives the REAL dual-confirmation flow: both endpoints are owner-unknown in the
upload flow, so TWO DISTINCT platform-admins must confirm (one side each) — a single confirmer
never reaches VERIFIED.
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime

from tests.featuregen.overlay.upload.passc.conftest import (
    _confirm_join,
    _drain,
    _propose_join,
    _reject_join,
)

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.identity import ApprovedJoinRef, ColumnPair, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.lifecycle import (
    Action,
    build_join_ref,
    candidate_fingerprint,
    decide_action,
    unordered_pair,
)
from featuregen.overlay.upload.passc.projection import list_approved_join_refs
from featuregen.runtime.observability import counters

_CIF_TERM = "Customer Information File Identifier"
_NOW = datetime(2026, 7, 13, tzinfo=UTC)


def _c(table, column, **kw):
    b = dict(object_ref=f"src::public.{table}.{column}", table=table, column=column,
             data_type="text", term_name="", term_type="", concept="", synonyms="",
             bian_leaf="", fibo_leaf="", table_entity="", column_entity="",
             data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


def _evidence(a, b):
    pairs = block_candidates([a, b])
    assert len(pairs) == 1, "test setup must yield exactly one blocked pair"
    return score(pairs[0], source_snapshot_id="snap-1")


def _cif_evidence(from_table="transactions", to_table="customers", column="cif_id"):
    """A strong, grain-inferred N:1 candidate: {from_table}.{column} -> {to_table}.{column}."""
    return _evidence(_c(from_table, column, term_name=_CIF_TERM),
                     _c(to_table, column, term_name=_CIF_TERM, is_grain=True))


def _ledger_insert(conn, evidence, *, key=None, lifecycle="proposed",
                   bucket=None, namespace=None, source="src"):
    """Simulate the Task-10 ledger write: one row per UNORDERED (sorted) column-ref pair."""
    lo, hi = unordered_pair(evidence)
    conn.execute(
        "INSERT INTO pass_c_candidate_evidence (catalog_source, candidate_id,"
        " candidate_fingerprint, from_ref, to_ref, fact_key, proposed_event_id, bucket,"
        " namespace_compatibility, lifecycle, evidence_json, source_snapshot_id, config_version,"
        " candidate_algorithm_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (source, evidence.candidate_id, candidate_fingerprint(evidence), lo, hi, key, None,
         bucket or evidence.bucket, namespace or evidence.namespace_compatibility.value,
         lifecycle, json.dumps(asdict(evidence)), evidence.source_snapshot_id,
         evidence.config_version, evidence.candidate_algorithm_version))


# ── build_join_ref ─────────────────────────────────────────────────────────────────────────────


def test_build_join_ref_parses_column_endpoints():
    ev = _cif_evidence()
    ref = build_join_ref(ev, "src")
    assert ref.from_ref.catalog_source == "src" and ref.to_ref.catalog_source == "src"
    assert ref.from_ref.object_kind == "column" and ref.to_ref.object_kind == "column"
    assert (ref.from_ref.schema, ref.from_ref.table, ref.from_ref.column) == \
        ("public", "transactions", "cif_id")
    assert (ref.to_ref.schema, ref.to_ref.table, ref.to_ref.column) == \
        ("public", "customers", "cif_id")
    assert ref.column_pairs[0].from_col == "cif_id" and ref.column_pairs[0].to_col == "cif_id"
    assert ref.cardinality == "N:1"


# ── candidate_fingerprint ──────────────────────────────────────────────────────────────────────


def test_fingerprint_stable_and_material_change_sensitive():
    ev = _cif_evidence()
    assert candidate_fingerprint(ev) == candidate_fingerprint(_cif_evidence())  # deterministic
    # snapshot id is NOT material (re-ingest of the same content dedupes) …
    assert candidate_fingerprint(replace(ev, source_snapshot_id="snap-2")) \
        == candidate_fingerprint(ev)
    # … but bucket / cardinality are.
    assert candidate_fingerprint(replace(ev, bucket="weak")) != candidate_fingerprint(ev)
    assert candidate_fingerprint(replace(ev, proposed_cardinality="1:1")) \
        != candidate_fingerprint(ev)


# ── decide_action ──────────────────────────────────────────────────────────────────────────────


def test_absent_candidate_proposes(passc_conn):
    ev = _cif_evidence()
    assert decide_action(passc_conn, build_join_ref(ev, "src"), ev) is Action.PROPOSE


def test_draft_same_fingerprint_skips_active(passc_conn):
    ev = _cif_evidence()
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev)                      # DRAFT, same fingerprint
    _ledger_insert(passc_conn, ev, key=fact_key(ref, "approved_join"))
    assert decide_action(passc_conn, ref, ev) is Action.SKIP_ACTIVE


def test_verified_skips_active(passc_conn, human_admin_1, human_admin_2):
    ev = _cif_evidence()
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev)
    # Dual confirmation: both sides owner-unknown -> TWO side-labelled platform-admin tasks;
    # admin1 -> PARTIALLY_CONFIRMED, DISTINCT admin2 -> VERIFIED (asserted inside the helper).
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)
    _ledger_insert(passc_conn, ev, key=fact_key(ref, "approved_join"))
    assert decide_action(passc_conn, ref, ev) is Action.SKIP_ACTIVE


def _upload_actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _cif_upload_rows(grain_table: str) -> list[CanonicalRow]:
    """Two tables share cif_id; `grain_table`'s side is the confirmed grain, so the scorer infers
    N:1 TOWARD it — flipping `grain_table` flips the direction, i.e. a DIFFERENT fact_key on the
    SAME unordered column pair."""
    return [CanonicalRow("bank", "customer", "cif_id", "integer",
                         is_grain=grain_table == "customer"),
            CanonicalRow("bank", "transactions", "cif_id", "integer",
                         is_grain=grain_table == "transactions")]


def _cif_upload_glossary() -> GlossaryUpload:
    def rec(table):
        return GlossaryRecord(
            logical_ref=normalize_ref("bank", "public", table, "cif_id"),
            term_name=_CIF_TERM, definition=f"The {_CIF_TERM}.", domain="Customer",
            synonyms=("CIF",), bian_path="Customer Management/Customer Reference", fibo_path="")
    return GlossaryUpload(rows=[], records=[rec("customer"), rec("transactions")])


def _conflict_count() -> int:
    return counters.snapshot()["counters"].get("overlay.passc.propose.conflict", 0)


def _ingest_cycle(conn, grain_table: str):
    res = ingest_upload(conn, "bank", _cif_upload_rows(grain_table), actor=_upload_actor(),
                        now=_NOW, glossary=_cif_upload_glossary())
    assert res.status == "ingested"
    _drain(conn)


def test_same_column_pair_different_fact_key_active_is_conflict(passc_conn, monkeypatch):
    """Cross-cycle CONFLICT through the REAL ingest flow (whole-branch review, Important-1).

    Cycle 1 ingests with customer.cif_id the confirmed grain: Pass C proposes
    transactions.cif_id -> customer.cif_id (fact_key F1), which stays an ACTIVE DRAFT awaiting
    the dual human gate. Cycle 2 re-ingests the SAME source with the grain FLIPPED
    (transactions.cif_id), so the scorer infers the OPPOSITE direction — a DIFFERENT fact_key
    F2 on the SAME unordered column pair. `_run_pass_c`'s clear-then-write must CARRY the
    pair's prior fact_key across the cycle (decide_action reads the ledger row's prior key), so
    the rival hits CONFLICT: the counter fires, NO second contradictory DRAFT is dispatched, no
    second set of gate tasks opens, and F1 stays the pair's sole claim. Hand-seeding the ledger
    here would prove nothing — the clear-then-write ordering is exactly what this test pins.

    (The DRAFT rival is THE cross-cycle conflict window: a VERIFIED F1 never reaches this check
    on a grain flip because the drift scan already demoted it — see the sibling test below.)"""
    monkeypatch.setenv("OVERLAY_PASS_C", "1")

    # Cycle 1: propose F1 through the real ingest; leave it DRAFT (the humans have not ruled).
    _ingest_cycle(passc_conn, "customer")
    refs = list_approved_join_refs(passc_conn, "bank")
    assert len(refs) == 1, refs
    f1_ref = refs[0]
    assert (f1_ref.from_ref.table, f1_ref.to_ref.table) == ("transactions", "customer")
    f1 = fact_key(f1_ref, "approved_join")
    assert fold_overlay_state(load_fact(passc_conn, f1)).status == "DRAFT"
    tasks_before = passc_conn.execute("SELECT count(*) FROM human_tasks").fetchone()[0]

    # Cycle 2: the SAME source, grain flipped -> the rival direction F2 for the same pair.
    before = _conflict_count()
    _ingest_cycle(passc_conn, "transactions")

    assert _conflict_count() == before + 1                  # the conflict was counted (+ logged)
    f2_ref = ApprovedJoinRef(from_ref=f1_ref.to_ref, to_ref=f1_ref.from_ref,
                             column_pairs=(ColumnPair("cif_id", "cif_id"),), cardinality="N:1")
    f2 = fact_key(f2_ref, "approved_join")
    assert f2 != f1
    assert load_fact(passc_conn, f2) == []                  # NO second contradictory DRAFT
    refs2 = list_approved_join_refs(passc_conn, "bank")     # one proposal ever — F1's
    assert [fact_key(r, "approved_join") for r in refs2] == [f1]
    assert fold_overlay_state(load_fact(passc_conn, f1)).status == "DRAFT"   # F1 untouched
    # No second pair of governance gate tasks was opened for the rival.
    assert passc_conn.execute(
        "SELECT count(*) FROM human_tasks").fetchone()[0] == tasks_before
    # The re-written ledger row KEPT the pair's governing claim (F1) — not NULL, not F2.
    assert passc_conn.execute(
        "SELECT fact_key FROM pass_c_candidate_evidence WHERE catalog_source='bank'"
    ).fetchall() == [(f1,)]


def test_verified_rival_resolves_via_drift_stale_and_repropose_not_conflict(
        passc_conn, monkeypatch, human_admin_1, human_admin_2):
    """The VERIFIED variant of the grain-flip is NOT a CONFLICT — by design, and this pins it.

    A grain flip changes both columns' safety fingerprints, so cycle 2's drift scan
    (`detect_catalog_changes`) STALEs the VERIFIED F1 BEFORE Pass C runs (the third exit from
    VERIFIED). `decide_action` then sees a demoted rival — which must NOT block the corrective
    proposal — so the new direction F2 is dispatched as a fresh DRAFT and the ledger's claim
    moves to F2. The carried-forward fact_key must not turn this correction path into a false
    CONFLICT."""
    monkeypatch.setenv("OVERLAY_PASS_C", "1")

    _ingest_cycle(passc_conn, "customer")
    f1_ref = list_approved_join_refs(passc_conn, "bank")[0]
    f1 = fact_key(f1_ref, "approved_join")
    _confirm_join(passc_conn, f1_ref, admin1=human_admin_1, admin2=human_admin_2)  # VERIFIED

    before = _conflict_count()
    _ingest_cycle(passc_conn, "transactions")

    assert _conflict_count() == before                      # no conflict: the rival was demoted
    assert fold_overlay_state(load_fact(passc_conn, f1)).status == "STALE"  # drift demotion
    f2_ref = ApprovedJoinRef(from_ref=f1_ref.to_ref, to_ref=f1_ref.from_ref,
                             column_pairs=(ColumnPair("cif_id", "cif_id"),), cardinality="N:1")
    f2 = fact_key(f2_ref, "approved_join")
    assert fold_overlay_state(load_fact(passc_conn, f2)).status == "DRAFT"  # corrective proposal
    # The ledger row's claim moved to the corrective proposal.
    assert passc_conn.execute(
        "SELECT fact_key FROM pass_c_candidate_evidence WHERE catalog_source='bank'"
    ).fetchall() == [(f2,)]


def test_different_column_pair_same_tables_is_not_a_conflict(passc_conn):
    ev = _cif_evidence()                                    # pair: *.cif_id <-> *.cif_id
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev)
    _ledger_insert(passc_conn, ev, key=fact_key(ref, "approved_join"))
    # A second join between the SAME tables on DIFFERENT columns is legitimate -> PROPOSE.
    other = _cif_evidence(column="branch_id")
    assert unordered_pair(other) != unordered_pair(ev)
    assert decide_action(passc_conn, build_join_ref(other, "src"), other) is Action.PROPOSE


def test_rejected_with_materially_changed_bucket_reproposes(passc_conn, human_admin_1):
    ev = _cif_evidence()
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev)
    _reject_join(passc_conn, ref, admin=human_admin_1)      # terminal REJECTED
    # Prior ledger row recorded the candidate as WEAK; the new evidence is STRONG -> REPROPOSE.
    _ledger_insert(passc_conn, ev, key=fact_key(ref, "approved_join"),
                   lifecycle="rejected", bucket="weak")
    assert ev.bucket == "strong"
    assert decide_action(passc_conn, ref, ev) is Action.REPROPOSE
