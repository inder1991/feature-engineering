"""Task 10 — glossary ingest wiring: re-upload staleness, human-confirmation revalidation, conflicts,
and the existing-path guard (must-prove #7/#8; review fixes #4/#5/#6/#16).

Drives the real ``ingest_upload`` end-to-end (the integration surface) with a glossary sidecar, and
proves:
  * a CHANGED definition STALEs the old ``definition@source`` evidence and writes a new ACTIVE one,
    while an UNCHANGED column's evidence is REUSED (same input_hash, not re-written) though the
    ``source_snapshot_id`` advanced (producer-scoped staleness, review #7);
  * a human-confirmed ``sensitivity`` SURVIVES a re-upload, and when the column's material changed a
    ``field_revalidation`` row is written + ``active_disqualifiers_for`` returns
    ``{CONFIRMATION_PENDING_REVALIDATION}`` so resolution BLOCKS the load-bearing value — the human
    evidence is NOT staled (must-prove #8);
  * a same-FQN conflicting-definition upload opens a ``conflict_review`` item (review #12/#16);
  * a non-glossary upload writes NO field_evidence / revalidation / conflict rows (guarded — the
    existing path is byte-for-byte unchanged).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import Disqualifier
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import is_feature_eligible, resolve_and_project
from featuregen.overlay.upload.field_revalidation import active_disqualifiers_for
from featuregen.overlay.upload.glossary_reader import read_glossary
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref

_SOURCE = "gloss"
_BAL_REF = normalize_ref(_SOURCE, "public", "accounts", "balance")
_STATUS_REF = normalize_ref(_SOURCE, "public", "accounts", "status")
_HEADER = ("physical_name,business_term,description_business_definition,data_domain,"
           "bian_path,fibo_path\n")


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _glossary_csv(balance_def: str, status_def: str = "The account status.") -> str:
    return (_HEADER
            + f"public.accounts.balance,Account Balance,{balance_def},Deposits,,\n"
            + f"public.accounts.status,Account Status,{status_def},Deposits,,\n")


def _ingest(db, csv_text: str, now: datetime) -> None:
    upload = read_glossary(csv_text, source=_SOURCE)
    # client=None isolates SOURCE/PARSER evidence (no LLM concepts / taxonomy) for the staleness proofs.
    res = ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=now,
                        client=None, glossary=upload)
    assert res.status == "ingested"


def _all_definition_rows(db, ref: str) -> list[tuple[str, str]]:
    """(proposed_value, lifecycle) for EVERY definition@source row (active or staled), oldest first."""
    return db.execute(
        "SELECT proposed_value, lifecycle FROM field_evidence "
        "WHERE logical_ref = %s AND field_name = 'definition' AND producer = 'source' "
        "ORDER BY created_at, evidence_id", (ref,)).fetchall()


NOW = datetime(2026, 7, 12, tzinfo=UTC)


def test_changed_definition_stales_old_source_evidence_unchanged_is_reused(db):
    _seal()
    _ingest(db, _glossary_csv("The ledger balance."), NOW)

    # Upload 1 wrote exactly one ACTIVE definition@source per column.
    bal1 = read_active_field_evidence(db, _BAL_REF, "definition")
    status1 = read_active_field_evidence(db, _STATUS_REF, "definition")
    assert len(bal1) == 1 and bal1[0].proposed_value == "The ledger balance."
    assert len(status1) == 1
    status_id_1, status_hash_1 = status1[0].evidence_id, status1[0].input_hash
    snap1 = bal1[0].source_snapshot_id

    # Re-upload: balance definition CHANGED, status definition UNCHANGED. New ingestion run.
    _ingest(db, _glossary_csv("The AVAILABLE balance (revised)."), NOW)

    # balance: the OLD definition@source is STALE, exactly one NEW ACTIVE row carries the new value.
    bal_rows = _all_definition_rows(db, _BAL_REF)
    assert ("The ledger balance.", "stale") in bal_rows
    bal_active = read_active_field_evidence(db, _BAL_REF, "definition")
    assert len(bal_active) == 1
    assert bal_active[0].proposed_value == "The AVAILABLE balance (revised)."
    assert bal_active[0].source_snapshot_id != snap1        # a NEW ingestion-run snapshot id

    # status: UNCHANGED input -> the SAME row is REUSED (not re-written, not staled), even though the
    # upload's source_snapshot_id advanced.
    status2 = read_active_field_evidence(db, _STATUS_REF, "definition")
    assert len(status2) == 1
    assert status2[0].evidence_id == status_id_1            # the identical row (reused)
    assert status2[0].input_hash == status_hash_1
    assert status2[0].source_snapshot_id == snap1           # still the first run's snapshot (not rewritten)


def test_human_confirmed_sensitivity_survives_and_blocks_on_material_change(db):
    _seal()
    _ingest(db, _glossary_csv("The ledger balance."), NOW)

    # A human confirms this column's sensitivity, then it resolves as CERTIFIED (load-bearing).
    record_field_evidence(
        db, logical_ref=_BAL_REF, field_name="sensitivity", proposed_value="restricted",
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref="human-review", source_snapshot_id="human-1",
        input_hash=field_input_hash(logical_ref=_BAL_REF, field_name="sensitivity",
                                    material="restricted"))
    human_id = read_active_field_evidence(db, _BAL_REF, "sensitivity")[0].evidence_id
    resolve_and_project(db, source=_SOURCE, logical_refs=[_BAL_REF], now=NOW)
    (status_before,) = db.execute(
        "SELECT classification_status FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = 'public.accounts.balance'", (_SOURCE,)).fetchone()
    assert status_before == "confirmed"                    # certified before the material change

    # Re-upload with a CHANGED definition -> material change flags sensitivity PENDING revalidation.
    _ingest(db, _glossary_csv("The AVAILABLE balance (revised)."), NOW)

    # A field_revalidation row was written and the disqualifier is now active for sensitivity.
    pending = db.execute(
        "SELECT count(*) FROM field_revalidation WHERE logical_ref = %s AND field_name = 'sensitivity' "
        "AND status = 'pending'", (_BAL_REF,)).fetchone()[0]
    assert pending == 1
    assert active_disqualifiers_for(db, _BAL_REF, "sensitivity") == frozenset(
        {Disqualifier.CONFIRMATION_PENDING_REVALIDATION})

    # The human evidence SURVIVED — NOT staled by the source re-upload.
    surviving = read_active_field_evidence(db, _BAL_REF, "sensitivity")
    assert [e.evidence_id for e in surviving] == [human_id]

    # ...and resolution now BLOCKS the load-bearing value pending re-confirmation.
    (status_after,) = db.execute(
        "SELECT classification_status FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = 'public.accounts.balance'", (_SOURCE,)).fetchone()
    assert status_after == "proposed"                      # blocked (was 'confirmed')
    assert is_feature_eligible(db, _BAL_REF, "sensitivity") is False


def test_same_fqn_conflicting_definition_opens_conflict_review(db):
    _seal()
    # Two rows name the SAME column with a DIFFERENT definition — validate_rows dedups them to one
    # attachable row, but the disagreement is a metadata conflict (review #12) opened for review.
    csv_text = (_HEADER
                + "public.accounts.balance,Account Balance,The ledger balance.,Deposits,,\n"
                + "public.accounts.balance,Account Balance,A DIFFERENT definition.,Deposits,,\n")
    upload = read_glossary(csv_text, source=_SOURCE)
    res = ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=NOW, client=None,
                        glossary=upload)
    assert res.status == "ingested"

    conflict = db.execute(
        "SELECT field_name, state FROM conflict_review WHERE logical_ref = %s", (_BAL_REF,)).fetchone()
    assert conflict is not None
    assert conflict[0] == "definition" and conflict[1] == "open"


def test_non_glossary_upload_writes_no_evidence_revalidation_or_conflict(db):
    """Guard (existing path unchanged): a technical upload (glossary=None) takes NONE of the glossary
    wiring — no field_evidence, no field_revalidation, no conflict_review rows."""
    _seal()
    rows = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
            CanonicalRow("deposits", "accounts", "balance", "numeric")]
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=NOW)   # no profile, no glossary
    assert res.status == "ingested" and res.asserted >= 1

    assert db.execute("SELECT count(*) FROM field_evidence").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM field_revalidation").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM conflict_review").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM field_decision_event").fetchone()[0] == 0
