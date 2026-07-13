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

from tests.featuregen.overlay.upload.passc.conftest import (
    _confirm_join,
    _propose_join,
    _reject_join,
)

from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.lifecycle import (
    Action,
    build_join_ref,
    candidate_fingerprint,
    decide_action,
    unordered_pair,
)

_CIF_TERM = "Customer Information File Identifier"


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


def test_same_column_pair_different_fact_key_active_is_conflict(passc_conn):
    ev = _cif_evidence()
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev)                      # ACTIVE (DRAFT) under fact_key F1
    _ledger_insert(passc_conn, ev, key=fact_key(ref, "approved_join"))
    # SAME unordered column pair, different cardinality -> a DIFFERENT fact_key: CONFLICT.
    rival = replace(ev, proposed_cardinality="1:1")
    rival_ref = build_join_ref(rival, "src")
    assert fact_key(rival_ref, "approved_join") != fact_key(ref, "approved_join")
    assert decide_action(passc_conn, rival_ref, rival) is Action.CONFLICT


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
