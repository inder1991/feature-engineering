"""Task 10 — end-to-end glossary ingest: rows -> all four producers' evidence -> resolve-and-project
-> graph_node display + authority -> a cause-labelled readiness diagnostic.

Drives the real ``ingest_upload`` with a glossary sidecar + a configured (fake) LLM in batch mode and
proves the whole spine connects: SOURCE (definition), PARSER (logical_representation/semantic_type),
LLM (concept) and TAXONOMY (behavioural) evidence all land, resolve-and-project projects the DISPLAY
value into ``graph_node`` while ``is_feature_eligible`` reads AUTHORITY from the decision (display ≠
authority), and ``compute_readiness`` returns a cause-labelled diagnostic.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.field_resolution import is_feature_eligible
from featuregen.overlay.upload.glossary_reader import read_glossary
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.readiness import (
    CAUSE_NOT_PROMOTED,
    ReadinessScopeType,
    compute_readiness,
)

_SOURCE = "gloss"
_BAL_REF = normalize_ref(_SOURCE, "public", "accounts", "balance")
_OBJECT_REF = "public.accounts.balance"
_CONCEPT_TASK = "overlay.enrich.concept"
_DOMAIN_TASK = "overlay.enrich.domain"

# A glossary whose business definition embeds an FTR sample profile with DECIMAL values, so the
# deterministic parser certifies logical_representation=decimal / semantic_type=amount (parser/supported).
_BAL_DEF = ("The ledger balance of the account. The sample profile is NUMERIC, with representative "
            "values such as 1250.00; 9.99; 42.50, which supports interpretation.")
_CSV = ("physical_name,business_term,description_business_definition,data_domain,bian_path,fibo_path\n"
        f"public.accounts.balance,Account Balance,{_BAL_DEF},Deposits,Product/CurrentAccount,"
        "fibo-fbc:Balance\n")

NOW = datetime(2026, 7, 12, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _node(db, *cols):
    return db.execute(
        f"SELECT {', '.join(cols)} FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s", (_SOURCE, _OBJECT_REF)).fetchone()


def test_glossary_ingest_end_to_end(db, monkeypatch):
    _seal()
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")   # batch mode writes LLM concept evidence

    upload = read_glossary(_CSV, source=_SOURCE)
    (bal_row,) = upload.rows
    h_bal = content_hash(bal_row)
    client = FakeLLM(script={
        _CONCEPT_TASK: FakeResponse(output={"results": [{"ref": h_bal, "concept": "monetary_stock"}]}),
        _DOMAIN_TASK: FakeResponse(output={"domain": "deposits"}),
    })

    res = ingest_upload(db, _SOURCE, upload.rows, actor=_actor(), now=NOW, client=client,
                        glossary=upload)
    assert res.status == "ingested"

    # 1) All four producers wrote evidence, keyed by the schema-preserving logical_ref.
    def _producers(field):
        return {e.producer for e in read_active_field_evidence(db, _BAL_REF, field)}
    assert "source" in _producers("definition")                      # SOURCE (attested)
    assert "parser" in _producers("logical_representation")          # PARSER (supported)
    assert "llm" in _producers("concept")                            # LLM (proposed)
    assert "taxonomy" in _producers("additivity")                    # TAXONOMY (derived, proposed)

    # 2) DISPLAY was projected into the flat graph_node columns.
    concept, definition, additivity = _node(db, "concept", "definition", "additivity")
    assert concept == "monetary_stock"                               # LLM display concept shown
    assert definition.startswith("The ledger balance of the account.")
    assert additivity == "semi_additive"                             # taxonomy-derived display

    # 3) DISPLAY ≠ AUTHORITY — is_feature_eligible reads the DECISION, not the flat column.
    assert is_feature_eligible(db, _BAL_REF, "concept") is False     # LLM-alone is never load-bearing
    assert is_feature_eligible(db, _BAL_REF, "additivity") is False  # derived from a PROPOSED concept
    assert is_feature_eligible(db, _BAL_REF, "logical_representation") is True  # deterministic parser gates

    # 4) The sensitivity floor RESTRICTS but does not CERTIFY (no source/human classification).
    effective_restriction, classification_status = _node(
        db, "effective_restriction", "classification_status")
    assert effective_restriction == "public"                        # the monetary_stock floor
    assert classification_status == "proposed"                      # floor-only, not certified

    # 5) Readiness returns a cause-labelled diagnostic (spec §9).
    readiness = compute_readiness(db, source=_SOURCE, scope=ReadinessScopeType.CATALOG)
    assert readiness.operational_status == "blocked"                # grain/join are not promoted in Phase 1
    assert all(r.cause for r in readiness.blocking_requirements)    # every blocker carries a cause
    assert CAUSE_NOT_PROMOTED in {r.cause for r in readiness.blocking_requirements}
