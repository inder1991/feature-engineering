"""E1a Task 1: the field-parameterised ``llm/proposed`` evidence writer.

Proves the four properties the writer exists for:
  * a plain field write lands as ``producer=llm`` / ``strength=proposed`` evidence;
  * re-enrichment SUPERSEDES the prior LLM row (no reuse/cache — stale-all, then write fresh), so
    exactly one active row survives;
  * the TWO IDENTITIES stay separate — attachability is checked at the PUBLIC-flattened binding ref
    while the evidence is stored at the SCHEMA-preserving ref (collapsing them silently skips every
    non-``public``-schema column);
  * reconciliation retires prior AI evidence for a deliberately-withheld target but KEEPS it for a
    transient failure;
  * fail-soft is per ITEM and wraps the WHOLE body — a throwing ``ref_of`` counts one failure and
    the item AFTER it is still written.
"""
from __future__ import annotations

from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import (
    _reconcile_llm_field_evidence,
    _write_llm_field_evidence,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.upload_identity import classify_upload


def test_writes_generic_llm_proposed_evidence(overlay_conn):
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "notional", "numeric")])
    ref = f"{src}::public.trades.notional"
    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h1": "Notional amount of the trade"},
        ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    assert n == 0                      # zero failures
    ev = read_active_field_evidence(overlay_conn, ref, "definition")
    assert ev[0].producer == "llm" and ev[0].strength == "proposed"
    assert ev[0].proposed_value == "Notional amount of the trade"
    assert ev[0].producer_ref == "overlay-enrichment" and ev[0].producer_item_ref == "h1"


def test_reenrich_supersedes_prior_llm_evidence(overlay_conn):
    """Re-enrichment supersedes prior LLM evidence -> exactly one active row with the new value
    (no reuse optimization; the writer stales-all then writes fresh)."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "ccy", "text")])
    ref = f"{src}::public.trades.ccy"
    _write_llm_field_evidence(overlay_conn, field_name="definition",
                              items={"h": "Currency of the trade"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    _write_llm_field_evidence(overlay_conn, field_name="definition",
                              items={"h": "Settlement currency"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    active = read_active_field_evidence(overlay_conn, ref, "definition")
    assert len(active) == 1 and active[0].proposed_value == "Settlement currency"


def test_binding_checked_at_public_ref_stored_at_schema_ref(overlay_conn):
    """The regression guard for the two-identity bug: a non-public-schema column must NOT be skipped.
    The binding lives under the PUBLIC-flattened ref (that is how ``classify_upload`` keys a
    CanonicalRow, which carries no schema); the evidence must store under the SCHEMA-preserving ref."""
    src = "ftr"
    row = CanonicalRow(src, "accounts", "balance", "numeric")
    build_graph(overlay_conn, src, [row], schemas={"public.accounts.balance": "dpl_eib_compliance"})
    ev_ref = normalize_ref(src, "dpl_eib_compliance", "accounts", "balance")  # schema-preserving
    bind_ref = normalize_ref(src, None, "accounts", "balance")                # public-flattened
    assert ev_ref != bind_ref
    bindings, _ = classify_upload([row])          # the REAL classify path: EXACT, keyed by bind_ref
    assert bind_ref in bindings

    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h": "Account balance"},
        ref_of=lambda k: (ev_ref, bind_ref, {"k": k}), source_snapshot_id="snap",
        bindings=bindings)

    assert n == 0
    # Stored under the SCHEMA ref, NOT skipped (the bug would write nothing here):
    assert read_active_field_evidence(overlay_conn, ev_ref, "definition")
    assert not read_active_field_evidence(overlay_conn, bind_ref, "definition")


def test_withheld_target_retires_prior_ai(overlay_conn):
    """A column that HAD AI evidence but is deliberately withheld this run -> prior AI is RETIRED."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "note", "text")])
    ref = f"{src}::public.trades.note"
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "old AI def"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    _reconcile_llm_field_evidence(overlay_conn, field_name="definition", retire_refs={ref})
    assert read_active_field_evidence(overlay_conn, ref, "definition") == []


def test_transient_failure_keeps_prior_ai(overlay_conn):
    """A transient failure must NOT retire prior AI (its ref is excluded from ``retire_refs``)."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "memo", "text")])
    ref = f"{src}::public.trades.memo"
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "keep me"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    _reconcile_llm_field_evidence(overlay_conn, field_name="definition", retire_refs=set())
    assert read_active_field_evidence(overlay_conn, ref, "definition")


def test_fail_soft_item_after_failure_still_written(overlay_conn):
    """A ``ref_of`` that THROWS for one key must not abort the batch; the item after it is written."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "good", "text")])
    good = f"{src}::public.trades.good"

    def ref_of(k):
        if k == "bad":
            raise ValueError("boom")     # throws where the OLD narrow try wouldn't catch it
        return (good, good, {"k": k})

    n = _write_llm_field_evidence(overlay_conn, field_name="definition",
                                  items={"bad": "x", "good": "kept"},   # dict order: bad first
                                  ref_of=ref_of, source_snapshot_id="snap")
    assert n == 1                                        # one failure counted, not raised
    assert read_active_field_evidence(overlay_conn, good, "definition")
