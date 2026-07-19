"""Delivery B item 8 — technical-CSV SOURCE evidence for the declared per-column facts.

Before this, a technical upload's ``unit/currency/entity/sensitivity/additivity/definition`` landed
ONLY as ``graph_node`` flat columns (``build_graph``) with no ``field_evidence``/decision/authority —
so the declared values could never be governed or become operationally load-bearing. Now
``_ingest_technical_evidence`` (the non-glossary mirror of the glossary evidence path) writes each
declared value as ``source/attested`` evidence (TECHNICAL_CSV_PROFILE attests all six fields),
reconciles present->absent on re-upload, and resolve-and-projects so a real DECISION exists.

The glossary path is untouched: the technical writer is gated to ``glossary is None`` exactly as the
glossary writers are gated to ``is_glossary`` — a glossary upload must never double-write.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.field_decision import read_field_decisions
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import is_feature_eligible
from featuregen.overlay.upload.glossary_reader import read_glossary
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref

NOW = datetime(2026, 7, 19, tzinfo=UTC)

_SOURCE = "deposits_tech"
_BAL_REF = normalize_ref(_SOURCE, None, "accounts", "balance")


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _rows(*, unit: str = "dollars", currency: str = "USD") -> list[CanonicalRow]:
    return [
        CanonicalRow(_SOURCE, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(_SOURCE, "accounts", "balance", "numeric",
                     definition="The ledger balance of the account.",
                     sensitivity="restricted", additivity="additive",
                     unit=unit, currency=currency, entity="Account"),
    ]


def test_technical_upload_writes_attested_source_evidence_and_decisions(db):
    """A technical CSV's declared unit/currency/sensitivity (etc.) become source/attested evidence
    AND a resolved decision — no longer ONLY a graph_node flat column."""
    _seal()
    res = ingest_upload(db, _SOURCE, _rows(), actor=_actor(), now=NOW)
    assert res.status == "ingested"

    # 1) SOURCE evidence landed for every declared field, at ATTESTED (TECHNICAL_CSV_PROFILE).
    for field, value in (("unit", "dollars"), ("currency", "USD"), ("sensitivity", "restricted"),
                         ("additivity", "additive"), ("entity", "Account"),
                         ("definition", "The ledger balance of the account.")):
        evidence = read_active_field_evidence(db, _BAL_REF, field)
        assert evidence, f"no active evidence for {field}"
        assert evidence[0].producer == "source", field
        assert evidence[0].strength == "attested", field
        assert evidence[0].proposed_value == value, field
        assert evidence[0].producer_ref            # never empty: run id or minted fallback

    # 2) resolve_and_project produced a RESOLVED decision — the value is governed authority now,
    #    not just display. unit/currency are decision-only fields (no flat display column), so the
    #    decision log is the ONLY place they can be load-bearing.
    for field in ("unit", "currency"):
        decisions = read_field_decisions(db, _BAL_REF, field)
        assert decisions, f"no decision for {field}"
        assert decisions[-1].load_bearing_value_hash is not None, field
        assert is_feature_eligible(db, _BAL_REF, field) is True, field
    # additivity gates from source/attested too, and its decision link lands on the graph node.
    assert is_feature_eligible(db, _BAL_REF, "additivity") is True
    additivity, additivity_decision_id = db.execute(
        "SELECT additivity, additivity_decision_id FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = 'public.accounts.balance'",
        (_SOURCE,)).fetchone()
    assert additivity == "additive"                      # flat display (build_graph + projection)
    assert additivity_decision_id is not None            # display ≠ authority link — decision landed

    # 3) The source-attested sensitivity CERTIFIES the classification (spec §7): before this task a
    #    technical upload's sensitivity could never certify (no source evidence existed).
    effective_restriction, classification_status = db.execute(
        "SELECT effective_restriction, classification_status FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = 'public.accounts.balance'",
        (_SOURCE,)).fetchone()
    assert effective_restriction == "restricted"
    assert classification_status == "confirmed"
    assert is_feature_eligible(db, _BAL_REF, "sensitivity") is True

    # 4) An undeclared column wrote nothing (skip-empty): accounts.id declared none of the six.
    id_ref = normalize_ref(_SOURCE, None, "accounts", "id")
    for field in ("unit", "currency", "sensitivity", "additivity", "entity", "definition"):
        assert read_active_field_evidence(db, id_ref, field) == []


def test_reupload_dropping_a_field_stales_its_source_evidence(db):
    """Present->absent reconciliation (_stale_absent_fields over _TECHNICAL_SOURCE_FIELDS): a
    re-upload that no longer declares `unit` stales the prior active row; an unchanged `currency`
    is reused, not re-written."""
    _seal()
    assert ingest_upload(db, _SOURCE, _rows(), actor=_actor(), now=NOW).status == "ingested"
    assert read_active_field_evidence(db, _BAL_REF, "unit")

    res = ingest_upload(db, _SOURCE, _rows(unit=""), actor=_actor(), now=NOW)
    assert res.status == "ingested"
    assert read_active_field_evidence(db, _BAL_REF, "unit") == []       # dropped -> staled
    currency = read_active_field_evidence(db, _BAL_REF, "currency")
    assert len(currency) == 1                                           # reused, never duplicated
    assert currency[0].proposed_value == "USD"


# A schema-carrying glossary upload (the FTR norm). Glossary evidence keys the SCHEMA-PRESERVING
# ref; the technical writer would key the PUBLIC-FLATTENED one — so any row under the flat ref
# would prove the technical writer wrongly ran on the glossary path.
_GLOSS_SOURCE = "gloss_tech_guard"
_GLOSS_CSV = (
    "physical_name,business_term,description_business_definition,data_domain,bian_path,fibo_path\n"
    "dpl_eib_compliance.accounts.balance,Account Balance,The ledger balance of the account.,"
    "Deposits,Product/CurrentAccount,fibo-fbc:Balance\n")


def test_glossary_upload_does_not_invoke_the_technical_writer(db):
    _seal()
    upload = read_glossary(_GLOSS_CSV, source=_GLOSS_SOURCE)
    res = ingest_upload(db, _GLOSS_SOURCE, upload.rows, actor=_actor(), now=NOW, client=None,
                        glossary=upload)
    assert res.status == "ingested"

    # The glossary source writer ran exactly once: ONE active source definition row, ATTESTED,
    # keyed by the schema-preserving ref (unchanged glossary behavior — no regression).
    schema_ref = normalize_ref(_GLOSS_SOURCE, "dpl_eib_compliance", "accounts", "balance")
    definition = read_active_field_evidence(db, schema_ref, "definition")
    assert len(definition) == 1
    assert definition[0].producer == "source" and definition[0].strength == "attested"

    # And the TECHNICAL writer did NOT also run: zero evidence rows under the public-flattened ref
    # it would have keyed (glossary is not None -> the technical path is skipped entirely).
    flat_ref = normalize_ref(_GLOSS_SOURCE, None, "accounts", "balance")
    (n,) = db.execute(
        "SELECT count(*) FROM field_evidence WHERE logical_ref = %s", (flat_ref,)).fetchone()
    assert n == 0
