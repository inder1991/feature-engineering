"""Review M-2 — the glossary PARSER mirror of the technical decision-retire (b8254cc).

``_write_glossary_parser_evidence`` stales a dropped sample facet's PARSER evidence, but
``resolve_and_project`` iterates only fields with ACTIVE evidence — so the prior load-bearing
``logical_representation``/``semantic_type`` DECISION stayed the latest and ``is_feature_eligible``
kept serving a structural type the source no longer asserts. The generalized
``_retire_dropped_field_decisions(fields=_PARSER_FIELDS)`` (the same helper the technical path
uses) must retire that decision BEFORE the glossary round's resolve — guarded so a human-kept or
still-present field is untouched.

Drives the REAL ``ingest_upload`` twice with a glossary sidecar (the test_glossary_reupload.py
harness shape).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import read_field_decisions
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
)
from featuregen.overlay.upload.field_resolution import is_feature_eligible, resolve_and_project
from featuregen.overlay.upload.glossary_reader import read_glossary
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref

NOW = datetime(2026, 7, 19, tzinfo=UTC)

_SOURCE = "gloss_retire"
_BAL_REF = normalize_ref(_SOURCE, "public", "accounts", "balance")
_LIMIT_REF = normalize_ref(_SOURCE, "public", "accounts", "limit_amount")
_HEADER = ("physical_name,business_term,description_business_definition,data_domain,"
           "bian_path,fibo_path\n")

# A definition carrying the deterministic sample-profile facet the reader's parser certifies into
# logical_representation/semantic_type (parser:supported — OPERATIONAL, load-bearing).
_PROFILED_BAL = ("The ledger balance. The sample profile is NUMERIC, with representative values "
                 "such as 1250.00; 9.99; 42.50, which supports interpretation.")
_PROFILED_LIMIT = ("The credit limit amount. The sample profile is NUMERIC, with representative "
                   "values such as 500.00; 1000.00; 2500.00, which supports interpretation.")
_PLAIN_BAL = "The ledger balance (revised, no sample profile)."


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _glossary_csv(balance_def: str, limit_def: str = _PROFILED_LIMIT) -> str:
    return (_HEADER
            + f'public.accounts.balance,Account Balance,"{balance_def}",Deposits,,\n'
            + f'public.accounts.limit_amount,Credit Limit,"{limit_def}",Deposits,,\n')


def _ingest(db, csv_text: str) -> None:
    upload = read_glossary(csv_text, source=_SOURCE)
    # client=None isolates SOURCE/PARSER evidence (no LLM concepts / taxonomy) for these proofs.
    res = ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=NOW,
                        client=None, glossary=upload)
    assert res.status == "ingested"


def test_dropped_sample_facet_retires_load_bearing_parser_decision(db):
    """Upload 2 drops balance's sample facet -> the prior load-bearing PARSER decision is RETIRED
    (is_feature_eligible False, latest event STALED), while the sibling column that still carries
    its facet stays eligible."""
    _seal()
    _ingest(db, _glossary_csv(_PROFILED_BAL))

    # Upload 1 made the parsed shape load-bearing for BOTH columns.
    active1 = read_active_field_evidence(db, _BAL_REF, "logical_representation")
    assert len(active1) == 1 and active1[0].producer == "parser"
    assert is_feature_eligible(db, _BAL_REF, "logical_representation") is True
    assert is_feature_eligible(db, _LIMIT_REF, "logical_representation") is True

    # Upload 2: balance's edited definition NO LONGER carries the facet; limit_amount keeps its.
    _ingest(db, _glossary_csv(_PLAIN_BAL))

    # The dropped facet's EVIDENCE is staled (pre-existing behavior)...
    assert read_active_field_evidence(db, _BAL_REF, "logical_representation") == []
    assert read_active_field_evidence(db, _BAL_REF, "semantic_type") == []
    # ...and (M-2) its load-bearing DECISION is now retired too — the structural type the source
    # no longer asserts must not keep driving feature eligibility.
    assert is_feature_eligible(db, _BAL_REF, "logical_representation") is False
    assert is_feature_eligible(db, _BAL_REF, "semantic_type") is False
    latest = read_field_decisions(db, _BAL_REF, "logical_representation")[-1]
    assert latest.event_type == "staled"
    assert latest.load_bearing_value_hash is None

    # The still-present sibling field is untouched: active parser evidence + still eligible.
    assert read_active_field_evidence(db, _LIMIT_REF, "logical_representation") != []
    assert is_feature_eligible(db, _LIMIT_REF, "logical_representation") is True


def test_human_kept_parser_field_is_not_retired(db):
    """The producer-scoping guard: a field with active HUMAN evidence keeps the field resolvable —
    the retire helper must NOT stale its decision when the source drops the facet."""
    _seal()
    _ingest(db, _glossary_csv(_PROFILED_BAL))
    parsed_value = read_active_field_evidence(db, _BAL_REF, "logical_representation")[0] \
        .proposed_value

    # A human CONFIRMS the parsed shape between the two uploads.
    record_field_evidence(
        db, logical_ref=_BAL_REF, field_name="logical_representation",
        proposed_value=parsed_value, producer=EvidenceProducer.HUMAN,
        strength=AssertionStrength.CONFIRMED, producer_ref="human-review",
        source_snapshot_id="human-1",
        input_hash=field_input_hash(logical_ref=_BAL_REF, field_name="logical_representation",
                                    material=parsed_value))
    (human,) = [e for e in read_active_field_evidence(db, _BAL_REF, "logical_representation")
                if e.producer == "human"]
    resolve_and_project(db, source=_SOURCE, logical_refs=[_BAL_REF], now=NOW)

    # Upload 2 drops the facet — the parser evidence stales, but the HUMAN row stays active.
    _ingest(db, _glossary_csv(_PLAIN_BAL))

    surviving = read_active_field_evidence(db, _BAL_REF, "logical_representation")
    assert [e.evidence_id for e in surviving] == [human.evidence_id]
    # NOT retired: the latest decision is not a STALED retire (the material change flags the
    # confirmation pending revalidation instead — a review signal, never a silent stale).
    latest = read_field_decisions(db, _BAL_REF, "logical_representation")[-1]
    assert latest.event_type != "staled"
