"""Program-audit G2 (F2 Critical + F10): SOURCE-provenance four-eyes on the two governed
upload-authored confirm surfaces.

The cross-delivery bypass: a single platform-admin uploads a file that DECLARES a governed value
(a semantic binding's entity/currency shape, or the facets Pass B synthesizes grain/availability
from), the ingest stages propose it under the SERVICE enrichment actor (so ``proposer_ne_confirmer``
trivially passes against ANY human), and the SAME admin then confirms it alone — one human both
authored and approved a governed value.

Fix under test (the M-7 standard from ``field_correction`` replicated onto the overlay fact
surfaces): the ingest proposal stages record the uploading HUMAN principal as ``source_uploader``
on the OVERLAY_FACT_PROPOSED payload, and ``confirm_fact`` DENIES a confirmer whose subject equals
the recorded uploader. A DIFFERENT admin still single-confirms exactly as before.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen._helpers import mint_test_identity

from featuregen.contracts.envelopes import Command
from featuregen.overlay.commands import confirm_fact
from featuregen.overlay.facts import CURRENCY_BINDING
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref

_NOW = datetime(2026, 7, 20, tzinfo=UTC)
_SOURCE = "bank"

_UPLOADER = mint_test_identity(subject="user:admin-uploader", role_claims=("platform-admin",))
_SECOND_ADMIN = mint_test_identity(subject="user:admin-second", role_claims=("platform-admin",))


def _currency_rows() -> list[CanonicalRow]:
    """One measure + exactly one currency column -> a single STRONG currency_binding candidate."""
    return [
        CanonicalRow(_SOURCE, "transactions", "txn_id", "integer", is_grain=True),
        CanonicalRow(_SOURCE, "transactions", "amount", "numeric"),
        CanonicalRow(_SOURCE, "transactions", "currency", "text"),
    ]


def _passb_rows() -> list[CanonicalRow]:
    """No declared grain/as-of: only Pass B can propose them (nothing for _assert_fact to VERIFY)."""
    return [
        CanonicalRow(_SOURCE, "txn", "id", "integer"),
        CanonicalRow(_SOURCE, "txn", "posted_at", "timestamp"),
    ]


def _synth_client():
    from featuregen.intake.llm import FakeLLM, FakeResponse
    return FakeLLM(script={"table_synth": FakeResponse(output={"results": [
        {"ref": "txn", "synthesis": {"grain_columns": ["id"], "as_of_column": "posted_at",
                                     "as_of_basis": "posted_at", "table_role": "fact",
                                     "primary_entity": "transaction",
                                     "event_or_snapshot": "event"}}]})})


def _currency_fact_key() -> str:
    return fact_key(
        CatalogObjectRef(catalog_source=_SOURCE, object_kind="column", schema="public",
                         table="transactions", column="amount"),
        CURRENCY_BINDING)


def _confirm(conn, key: str, ref, fact_type: str, *, actor):
    """Dispatch the REAL confirm command exactly as the E2 / table-fact routes do."""
    ensure_upload_catalog_adapter()
    state = fold_overlay_state(load_fact(conn, key))
    return confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", key,
        {"ref": ref, "fact_type": fact_type, "target_event_id": state.draft_event_id},
        actor, f"confirm:{key}:{actor.subject}"))


def _ingest_semantic_binding_draft(db, monkeypatch) -> None:
    monkeypatch.setenv("OVERLAY_SEMANTIC_BINDING_CANDIDATES", "1")
    monkeypatch.setenv("OVERLAY_SEMANTIC_BINDING_PROPOSALS", "1")
    res = ingest_upload(db, _SOURCE, _currency_rows(), actor=_UPLOADER, now=_NOW)
    assert res.status == "ingested" and res.semantic_binding_proposed == 1


def _ingest_passb_draft(db, monkeypatch) -> None:
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    res = ingest_upload(db, _SOURCE, _passb_rows(), actor=_UPLOADER, client=_synth_client())
    assert res.status == "ingested"
    assert fold_overlay_state(load_fact(db, fact_key(table_ref(_SOURCE, "txn"), "grain"))
                              ).status == "DRAFT"


# ── F2 (Critical): E2 semantic-binding confirm ────────────────────────────────────────────────────

def test_f2_uploader_cannot_solo_confirm_semantic_binding(db, monkeypatch):
    """The admin who uploaded the file declaring the binding shape may NOT be its sole confirmer:
    a single-actor upload->confirm round-trip must be denied (four-eyes), never reach VERIFIED."""
    _ingest_semantic_binding_draft(db, monkeypatch)
    key = _currency_fact_key()
    ref = CatalogObjectRef(catalog_source=_SOURCE, object_kind="column", schema="public",
                           table="transactions", column="amount")

    res = _confirm(db, key, ref, CURRENCY_BINDING, actor=_UPLOADER)

    assert res.accepted is False, (
        "four-eyes bypass: the uploading admin confirmed their own upload-declared binding alone")
    assert "four-eyes" in (res.denied_reason or "")
    assert fold_overlay_state(load_fact(db, key)).status == "DRAFT"   # untouched, still human-gated


def test_f2_semantic_binding_proposal_records_the_uploader(db, monkeypatch):
    """The service proposal carries the uploading principal as provenance (`source_uploader`) while
    `proposed_by` stays the service actor — proposer-ne-confirmer holds vs any OTHER human."""
    _ingest_semantic_binding_draft(db, monkeypatch)
    stream = load_fact(db, _currency_fact_key())
    proposed = [e for e in stream if e.type == "OVERLAY_FACT_PROPOSED"]
    assert proposed[0].payload["proposed_by"] == "featuregen-overlay-enrichment"
    assert proposed[0].payload["source_uploader"] == _UPLOADER.subject


def test_f2_a_different_admin_still_single_confirms(db, monkeypatch):
    """The two-distinct-humans rule, not a workflow change: a DIFFERENT platform-admin confirms
    exactly as before and the binding reaches VERIFIED."""
    _ingest_semantic_binding_draft(db, monkeypatch)
    key = _currency_fact_key()
    ref = CatalogObjectRef(catalog_source=_SOURCE, object_kind="column", schema="public",
                           table="transactions", column="amount")

    res = _confirm(db, key, ref, CURRENCY_BINDING, actor=_SECOND_ADMIN)

    assert res.accepted, res.denied_reason
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"


# ── F10: Pass B table-fact confirm (grain / availability_time) ────────────────────────────────────

def test_f10_uploader_cannot_solo_confirm_pass_b_table_fact(db, monkeypatch):
    """Same hole, same rule on the Pass B surface: the admin whose upload shaped the grain/as-of
    operands may not single-handedly confirm the resulting governed table fact."""
    _ingest_passb_draft(db, monkeypatch)
    ref = table_ref(_SOURCE, "txn")
    key = fact_key(ref, "grain")

    res = _confirm(db, key, ref, "grain", actor=_UPLOADER)

    assert res.accepted is False, (
        "four-eyes bypass: the uploading admin confirmed the grain their own upload shaped")
    assert "four-eyes" in (res.denied_reason or "")
    assert fold_overlay_state(load_fact(db, key)).status == "DRAFT"


def test_f10_pass_b_proposal_records_the_uploader(db, monkeypatch):
    _ingest_passb_draft(db, monkeypatch)
    for ft in ("grain", "availability_time"):
        stream = load_fact(db, fact_key(table_ref(_SOURCE, "txn"), ft))
        proposed = [e for e in stream if e.type == "OVERLAY_FACT_PROPOSED"]
        assert proposed[0].payload["proposed_by"] == "featuregen-overlay-enrichment"
        assert proposed[0].payload["source_uploader"] == _UPLOADER.subject


def test_f10_a_different_admin_still_single_confirms(db, monkeypatch):
    _ingest_passb_draft(db, monkeypatch)
    ref = table_ref(_SOURCE, "txn")
    key = fact_key(ref, "grain")

    res = _confirm(db, key, ref, "grain", actor=_SECOND_ADMIN)

    assert res.accepted, res.denied_reason
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
