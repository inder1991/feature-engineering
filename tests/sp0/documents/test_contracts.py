from __future__ import annotations

import dataclasses

import pytest

from sp0.contracts.documents import (
    BODY_CLASSIFICATIONS,
    BRANCH_ROLES,
    STAGES,
    NewDocument,
    Stage,
)
from sp0.contracts.envelopes import ProvenanceEnvelope


def _prov() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="DRAFT_CONTRACT",
        schema_version=1,
        producing_component="sp0-test@0.0.0",
    )


def test_stage_enum_is_the_normative_published_set_in_order():
    assert STAGES == (
        "DRAFT_CONTRACT", "ASSUMPTION_LEDGER", "CONFIRMED_CONTRACT", "MAPPED_CONTRACT",
        "FEATURE_PLAN", "CANDIDATE_SQL", "VALIDATION_REPORT", "SANDBOX_RESULT", "DQ_REPORT",
        "EVALUATION_REPORT", "RISK_ASSESSMENT", "EXPLAINABILITY", "MONITORING_SPEC",
        "APPROVAL_RECORD",
    )
    assert Stage.CONFIRMED_CONTRACT.value == "CONFIRMED_CONTRACT"
    assert tuple(s.value for s in Stage) == STAGES


def test_branch_role_and_classification_vocab():
    assert BRANCH_ROLES == ("candidate", "primary", "rejected", "repair")
    assert BODY_CLASSIFICATIONS == ("pii-erasable", "governance-retained")


def test_new_document_is_frozen_with_defaults():
    doc = NewDocument(
        doc_id="doc_x",
        stage="DRAFT_CONTRACT",
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:abc",
        body_classification="pii-erasable",
        provenance=_prov(),
    )
    assert doc.body_ref is None
    assert doc.derived_from == () and doc.supersedes == ()
    assert doc.reject_reason is None
    assert dataclasses.is_dataclass(doc)
    with pytest.raises(dataclasses.FrozenInstanceError):
        doc.branch_role = "primary"  # type: ignore[misc]


def test_new_document_is_the_canonical_shared_type():
    # NewDocument is a shared contract type (single source of truth in envelopes).
    # documents.py must re-export the SAME class object, not a divergent copy.
    from sp0.contracts.envelopes import NewDocument as EnvNewDocument

    assert NewDocument is EnvNewDocument
