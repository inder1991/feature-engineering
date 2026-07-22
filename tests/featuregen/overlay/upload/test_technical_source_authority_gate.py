"""Delivery B ACCEPTANCE GATE — two-upload technical-CSV source-authority behavior, end-to-end.

Drives the REAL ``ingest_upload`` twice on the same connection (the technical, glossary-less path)
and closes Delivery B by proving the three source-authority behaviors on real state:

1. **A dropped technical field clears display + active evidence + load-bearing authority.** Upload 1
   declares ``currency="USD"``; upload 2 re-uploads the SAME column with the currency cell blank.
   The prior ``currency@source`` evidence must be STALED (nothing active), the ``graph_node`` flat
   ``currency`` display must be cleared, and the decision log must RETIRE the prior load-bearing
   decision (``is_feature_eligible`` false, latest event STALED) — a dropped value must not survive
   as a stale load-bearing value.
2. **Source/human evidence is untouched by an unrelated producer write.** A field the second upload
   STILL asserts (``unit``) keeps its exact active evidence row (reused, not re-written), and a HUMAN
   confirmation on an unrelated column survives the technical re-upload un-staled, un-flagged, and
   still operational (producer-scoped staleness).
3. **A material change flags a human-confirmed field CONFIRMATION_PENDING_REVALIDATION.** A human
   CONFIRMS ``currency`` after upload 1; upload 2 changes the column's MATERIAL (its definition) and
   drops the currency. The confirmation is flagged pending revalidation (``field_revalidation`` row +
   ``active_disqualifiers_for``), the human evidence itself is NOT staled, and its load-bearing
   effect is BLOCKED until a human re-confirms — never silently kept operational.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import Disqualifier
from featuregen.overlay.field_decision import read_field_decisions
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import is_feature_eligible, resolve_and_project
from featuregen.overlay.upload.field_revalidation import active_disqualifiers_for
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref

NOW = datetime(2026, 7, 19, tzinfo=UTC)

_SOURCE = "deposits_gate"
_BAL_REF = normalize_ref(_SOURCE, None, "accounts", "balance")
_STATUS_REF = normalize_ref(_SOURCE, None, "accounts", "status")


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _rows(*, currency: str = "USD",
          balance_def: str = "The ledger balance of the account.") -> list[CanonicalRow]:
    """One technical upload: the balance column declares the six source fields; status only some."""
    return [
        CanonicalRow(_SOURCE, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(_SOURCE, "accounts", "balance", "numeric",
                     definition=balance_def, sensitivity="restricted", additivity="additive",
                     unit="dollars", currency=currency, entity="Account"),
        CanonicalRow(_SOURCE, "accounts", "status", "text",
                     definition="The lifecycle status of the account.", entity="Account"),
    ]


def _ingest(db, rows: list[CanonicalRow]) -> None:
    res = ingest_upload(db, _SOURCE, rows, actor=_actor(), now=NOW)
    assert res.status == "ingested"


def _graph_currency(db):
    return db.execute(
        "SELECT currency FROM graph_node WHERE catalog_source = %s "
        "AND object_ref = 'public.accounts.balance'", (_SOURCE,)).fetchone()[0]


def _confirm_human(db, ref: str, field: str, value: str) -> str:
    """Seed a HUMAN CONFIRMED evidence row (the review-surface write) and return its evidence_id."""
    record_field_evidence(
        db, logical_ref=ref, field_name=field, proposed_value=value,
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref="human-review", source_snapshot_id="human-1",
        input_hash=field_input_hash(logical_ref=ref, field_name=field, material=value))
    (human,) = [e for e in read_active_field_evidence(db, ref, field) if e.producer == "human"]
    return human.evidence_id


def _pending_revalidations(db, ref: str, field: str) -> int:
    return db.execute(
        "SELECT count(*) FROM field_revalidation "
        "WHERE logical_ref = %s AND field_name = %s AND status = 'pending'",
        (ref, field)).fetchone()[0]


# ── Gate 1: a dropped technical field clears display + active evidence + load-bearing authority. ──
def test_dropped_currency_clears_evidence_display_and_load_bearing(db):
    _seal()
    _ingest(db, _rows())

    # Upload 1 made the declared currency GOVERNED authority: one active source/attested evidence
    # row, a load-bearing decision (feature-eligible), and the graph display value.
    (ev1,) = read_active_field_evidence(db, _BAL_REF, "currency")
    assert ev1.producer == "source" and ev1.strength == "attested"
    assert ev1.proposed_value == "USD"
    assert is_feature_eligible(db, _BAL_REF, "currency") is True
    assert _graph_currency(db) == "USD"

    # Upload 2: the SAME column with the currency cell dropped (blank).
    _ingest(db, _rows(currency=""))

    # (a) The prior SOURCE currency evidence is STALED — no active row remains, and the old row is
    # still in the log with lifecycle='stale' (retired, not deleted).
    assert read_active_field_evidence(db, _BAL_REF, "currency") == []
    lifecycles = {lc for (lc,) in db.execute(
        "SELECT lifecycle FROM field_evidence WHERE logical_ref = %s AND field_name = 'currency' "
        "AND producer = 'source'", (_BAL_REF,)).fetchall()}
    assert lifecycles == {"stale"}

    # (b) The graph_node display value is CLEARED, not silently retained.
    assert _graph_currency(db) is None

    # (c) The dropped value does NOT survive as a stale load-bearing value: the decision log RETIRED
    # the prior load-bearing decision (latest event is STALED, so eligibility fails closed).
    assert is_feature_eligible(db, _BAL_REF, "currency") is False
    latest = read_field_decisions(db, _BAL_REF, "currency")[-1]
    assert latest.event_type == "staled"
    assert latest.load_bearing_value_hash is None


# ── Gate 2: source/human evidence is untouched by an unrelated producer write. ──
def test_kept_unit_reused_and_unrelated_human_confirmation_untouched(db):
    _seal()
    _ingest(db, _rows())
    (unit1,) = read_active_field_evidence(db, _BAL_REF, "unit")
    unit_id = unit1.evidence_id

    # A human confirms sensitivity on an UNRELATED column (status) between the two uploads.
    human_id = _confirm_human(db, _STATUS_REF, "sensitivity", "internal")

    # Upload 2 drops balance.currency but STILL asserts unit (and status is unchanged).
    _ingest(db, _rows(currency=""))

    # The still-asserted unit kept its EXACT active evidence row — reused, never staled/duplicated.
    unit2 = read_active_field_evidence(db, _BAL_REF, "unit")
    assert [e.evidence_id for e in unit2] == [unit_id]
    assert unit2[0].proposed_value == "dollars"
    assert is_feature_eligible(db, _BAL_REF, "unit") is True

    # The HUMAN confirmation on the unrelated field SURVIVED the technical re-upload (staleness is
    # PRODUCER-scoped: a source write can never stale human evidence)...
    human2 = read_active_field_evidence(db, _STATUS_REF, "sensitivity")
    assert [e.evidence_id for e in human2] == [human_id]
    # ...was NOT flagged pending revalidation (status's material did not change)...
    assert _pending_revalidations(db, _STATUS_REF, "sensitivity") == 0
    assert active_disqualifiers_for(db, _STATUS_REF, "sensitivity") == frozenset()
    # ...and is still OPERATIONAL (the re-upload's resolve pass certified it, not blocked it).
    assert is_feature_eligible(db, _STATUS_REF, "sensitivity") is True


# ── Gate 3: a material change flags a human-confirmed field CONFIRMATION_PENDING_REVALIDATION. ──
def test_material_change_flags_human_confirmed_currency_pending_revalidation(db):
    _seal()
    _ingest(db, _rows())

    # A human CONFIRMS the currency after upload 1; it resolves as load-bearing authority.
    human_id = _confirm_human(db, _BAL_REF, "currency", "USD")
    resolve_and_project(db, source=_SOURCE, logical_refs=[_BAL_REF], now=NOW)
    assert is_feature_eligible(db, _BAL_REF, "currency") is True

    # Upload 2: the column's MATERIAL changed (definition restated) AND the currency was dropped.
    _ingest(db, _rows(currency="", balance_def="The AVAILABLE balance (restated)."))

    # The human confirmation is flagged CONFIRMATION_PENDING_REVALIDATION (field_revalidation row +
    # the resolver-facing disqualifier) — the exact flag field_revalidation.py emits.
    assert _pending_revalidations(db, _BAL_REF, "currency") == 1
    assert active_disqualifiers_for(db, _BAL_REF, "currency") == frozenset(
        {Disqualifier.CONFIRMATION_PENDING_REVALIDATION})

    # The human evidence itself was NOT staled (a source re-upload never stales human evidence) —
    # only the now-staled SOURCE row is gone from the active set.
    active = read_active_field_evidence(db, _BAL_REF, "currency")
    assert [e.evidence_id for e in active] == [human_id]

    # ...but its load-bearing effect is BLOCKED pending re-confirmation — the changed/dropped value
    # is NOT silently kept operational.
    assert is_feature_eligible(db, _BAL_REF, "currency") is False


# ── M-1: the technical material axis is ALL operational human-confirmable fields, not just the
# glossary-derived `definition`. A source currency change/drop with the definition UNCHANGED must
# still flag the human confirmation CONFIRMATION_PENDING_REVALIDATION — gate 3 above only passed
# because its upload 2 ALSO restated the definition. ──
def test_changed_currency_alone_flags_human_confirmed_currency_pending_revalidation(db):
    """Upload 1: human CONFIRMS currency=USD. Upload 2: the technical CSV re-declares the SAME
    column with currency=EUR and the definition UNCHANGED -> the stale human USD must be flagged
    pending revalidation, not silently kept operational with no review signal."""
    _seal()
    _ingest(db, _rows())
    human_id = _confirm_human(db, _BAL_REF, "currency", "USD")
    resolve_and_project(db, source=_SOURCE, logical_refs=[_BAL_REF], now=NOW)
    assert is_feature_eligible(db, _BAL_REF, "currency") is True

    # Upload 2: currency EUR, definition byte-for-byte UNCHANGED (the definition-only material
    # axis saw NO change here, so pre-fix the revalidation flag never fired).
    _ingest(db, _rows(currency="EUR"))

    # The human confirmation is flagged CONFIRMATION_PENDING_REVALIDATION (the pending-
    # revalidation row + the resolver-facing disqualifier).
    assert _pending_revalidations(db, _BAL_REF, "currency") == 1
    assert active_disqualifiers_for(db, _BAL_REF, "currency") == frozenset(
        {Disqualifier.CONFIRMATION_PENDING_REVALIDATION})

    # The human evidence itself is NOT staled — the disqualifier (not a stale) blocks it until a
    # human re-confirms against the source's new EUR assertion.
    active_ids = [e.evidence_id for e in read_active_field_evidence(db, _BAL_REF, "currency")]
    assert human_id in active_ids


def test_dropped_currency_alone_flags_human_confirmed_currency_pending_revalidation(db):
    """Present->absent variant of M-1: upload 2 DROPS the human-confirmed currency (blank cell)
    with the definition unchanged -> same flag; the stale human value must not stay operational."""
    _seal()
    _ingest(db, _rows())
    human_id = _confirm_human(db, _BAL_REF, "currency", "USD")
    resolve_and_project(db, source=_SOURCE, logical_refs=[_BAL_REF], now=NOW)
    assert is_feature_eligible(db, _BAL_REF, "currency") is True

    _ingest(db, _rows(currency=""))   # dropped; definition UNCHANGED

    assert _pending_revalidations(db, _BAL_REF, "currency") == 1
    assert active_disqualifiers_for(db, _BAL_REF, "currency") == frozenset(
        {Disqualifier.CONFIRMATION_PENDING_REVALIDATION})

    # The human evidence survived (producer-scoped staleness; the retire helper's active-HUMAN
    # guard also leaves the field alone) — but its load-bearing effect is BLOCKED.
    assert [e.evidence_id for e in read_active_field_evidence(db, _BAL_REF, "currency")] \
        == [human_id]
    assert is_feature_eligible(db, _BAL_REF, "currency") is False
