"""Task 9 — blocker-based, cause-labelled scoped readiness diagnostics (spec §9).

Proves the review-#13 must-fix: after resolve-and-project, :func:`compute_readiness` reports
blocking requirements LABELLED BY CAUSE so the report never conflates three very different
situations —

* ``not_promoted_in_phase1`` — grain / availability, which Phase 1 deliberately does not promote
  (EXPECTED, never an ingestion failure); the join dimension is now WIRED to live approved_join
  state and only blocks on a real CONFLICTING relationship;
* ``unresolved_authority`` — an OPERATIONAL field whose load-bearing value is unresolved because the
  active evidence's authority is insufficient (additivity awaiting a concept confirmation);
* ``ingestion_error`` — a genuine failure (irreconcilably conflicting evidence).

It is a DIAGNOSTIC: the gate is blocker-based (``operational_status == "blocked"`` iff any blocking
requirement exists) and ``summary_scores`` are display-only, derived from the requirement list.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.readiness import ReadinessScopeType, compute_readiness

_SOURCE = "deposits"


def _seed(db, ref, field_name, value, producer, strength, confidence=None):
    record_field_evidence(
        db,
        logical_ref=ref,
        field_name=field_name,
        proposed_value=value,
        producer=producer,
        strength=strength,
        producer_ref="test-producer",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=ref, field_name=field_name, material=value),
        confidence_band=confidence,
    )


@pytest.fixture
def resolved(db):
    """Two tables, resolved-and-projected, exercising each readiness bucket:

    * ``accounts.balance`` — ``concept`` LLM-proposed (review), ``logical_representation``
      parser-supported (confirmed / load-bearing), ``additivity`` from a PROPOSED taxonomy
      derivation (OPERATIONAL, load-bearing unresolved → ``unresolved_authority``).
    * ``customers.region`` — ``concept`` LLM-proposed (review), ``domain`` LLM-proposed at LOW
      confidence (an advisory gap, not worth a review ask).
    """
    balance = normalize_ref(_SOURCE, None, "accounts", "balance")
    region = normalize_ref(_SOURCE, None, "customers", "region")
    build_graph(
        db,
        _SOURCE,
        [
            CanonicalRow(_SOURCE, "accounts", "balance", "numeric"),
            CanonicalRow(_SOURCE, "customers", "region", "text"),
        ],
    )
    _seed(db, balance, "concept", "monetary_stock", EvidenceProducer.LLM, AssertionStrength.PROPOSED)
    _seed(
        db, balance, "logical_representation", "decimal",
        EvidenceProducer.PARSER, AssertionStrength.SUPPORTED,
    )
    _seed(
        db, balance, "additivity", "semi_additive",
        EvidenceProducer.TAXONOMY, AssertionStrength.PROPOSED,
    )
    _seed(db, region, "concept", "geo_region", EvidenceProducer.LLM, AssertionStrength.PROPOSED)
    _seed(
        db, region, "domain", "geography",
        EvidenceProducer.LLM, AssertionStrength.PROPOSED, confidence="low",
    )
    resolve_and_project(db, source=_SOURCE, logical_refs=[balance, region])
    return db, balance, region


def test_catalog_readiness_labels_blockers_by_cause(resolved):
    db, _balance, _region = resolved
    rep = compute_readiness(db, source=_SOURCE, scope=ReadinessScopeType.CATALOG)

    causes = {r.cause for r in rep.blocking_requirements}
    assert "unresolved_authority" in causes   # additivity (OPERATIONAL) awaiting concept confirmation
    assert "not_promoted_in_phase1" in causes  # grain / availability — EXPECTED, not a failure
    assert "ingestion_error" not in causes     # nothing genuinely failed in this scenario

    # The additivity blocker is attributed to unresolved_authority — never to error / not_promoted.
    additivity = [r for r in rep.blocking_requirements if r.requirement_id.endswith(":additivity")]
    assert additivity, "expected an additivity blocker"
    assert additivity[0].cause == "unresolved_authority"
    assert additivity[0].status == "proposed"

    # grain/availability blockers are labelled not_promoted_in_phase1 (expected), never
    # ingestion_error. (Phase 2: grain/availability now read the table's fact state — with no
    # Pass B proposal in this scenario they are still missing/not-promoted blockers. The join
    # dimension is WIRED to live approved_join state: with no relationships it reads NO_CANDIDATES
    # -> "confirmed" (satisfied) and must never be the old static per-table blocker — that
    # always-"blocked on joins" noise was the bug the wiring fixed.)
    structural = [r for r in rep.blocking_requirements if r.cause == "not_promoted_in_phase1"]
    assert {r.requirement_id.split(":")[0] for r in structural} == {"grain", "availability"}
    assert all(r.status == "missing" for r in structural)
    assert not any(r.requirement_id.startswith("join:") for r in rep.blocking_requirements)

    # A proposed-unconfirmed concept -> a REVIEW requirement (non-blocking), never a blocker.
    assert any(r.requirement_id.endswith(":concept") for r in rep.review_requirements)
    assert all(not r.blocking for r in rep.review_requirements)
    assert all(r.status == "proposed" for r in rep.review_requirements)
    assert not any(r.requirement_id.endswith(":concept") for r in rep.blocking_requirements)

    # A LOW-confidence domain proposal -> an advisory gap (not a review requirement, not a blocker).
    assert any("domain" in g for g in rep.advisory_gaps)
    assert not any(r.requirement_id.endswith(":domain") for r in rep.review_requirements)
    assert not any(r.requirement_id.endswith(":domain") for r in rep.blocking_requirements)

    # summary_scores are present but DISPLAY-ONLY: the gate is blocked even though the confirmed
    # fraction is positive (a load-bearing logical_representation resolved).
    assert rep.summary_scores
    assert "ready_fraction" in rep.summary_scores
    assert rep.summary_scores["ready_fraction"] > 0.0
    assert rep.operational_status == "blocked"


def test_table_scope_subsets_to_one_table(resolved):
    db, _balance, _region = resolved
    rep = compute_readiness(
        db, source=_SOURCE, scope=ReadinessScopeType.TABLE, subset="accounts"
    )
    assert rep.scope == ReadinessScopeType.TABLE

    ids = {r.requirement_id for r in rep.blocking_requirements} | {
        r.requirement_id for r in rep.review_requirements
    }
    # accounts fields + accounts structural facts present...
    assert any(i.endswith(":additivity") for i in ids)
    assert any(i.startswith("grain:") and "accounts" in i for i in ids)
    # ...but NOT the join requirement: wired to live approved_join state, a table with no
    # relationships is satisfied (NO_CANDIDATES -> "confirmed"), so it surfaces in neither
    # actionable list (pre-wiring it was a false static blocker on every table).
    assert not any(i.startswith("join:") for i in ids)
    # ...customers.region is excluded by the subset (fields AND its advisory gap).
    assert not any("region" in i for i in ids)
    assert not any("customers" in i for i in ids)
    assert not any("region" in g for g in rep.advisory_gaps)


def test_unresolved_authority_blocks_but_is_attributed_not_to_error(resolved):
    db, _balance, _region = resolved
    rep = compute_readiness(db, source=_SOURCE, scope=ReadinessScopeType.CATALOG)

    assert rep.operational_status == "blocked"
    additivity = next(
        r for r in rep.blocking_requirements if r.requirement_id.endswith(":additivity")
    )
    assert additivity.blocking is True
    assert additivity.cause == "unresolved_authority"   # clearly NOT an ingestion failure
    assert additivity.cause != "ingestion_error"


def test_conflicting_evidence_is_labelled_ingestion_error(db):
    ref = normalize_ref(_SOURCE, None, "ledger", "amount")
    build_graph(db, _SOURCE, [CanonicalRow(_SOURCE, "ledger", "amount", "numeric")])
    # Two SOURCE-attested representations that irreconcilably disagree -> a genuine conflict, not a
    # mere authority gap: PREFER_CONFIRMED ties at the top strength and cannot pick a single value.
    _seed(
        db, ref, "logical_representation", "decimal",
        EvidenceProducer.SOURCE, AssertionStrength.ATTESTED,
    )
    _seed(
        db, ref, "logical_representation", "text",
        EvidenceProducer.SOURCE, AssertionStrength.ATTESTED,
    )
    resolve_and_project(db, source=_SOURCE, logical_refs=[ref])

    rep = compute_readiness(db, source=_SOURCE, scope=ReadinessScopeType.CATALOG)
    req = next(
        r for r in rep.blocking_requirements
        if r.requirement_id.endswith(":logical_representation")
    )
    assert req.status == "conflicting"
    assert req.cause == "ingestion_error"          # a genuine failure...
    assert req.cause != "unresolved_authority"     # ...NOT conflated with an authority gap
    assert rep.operational_status == "blocked"


def test_table_subset_is_schema_aware(db):
    """Two schemas in one source share the table name 'accounts'. A schema-qualified TABLE subset
    must cover ONLY the intended (schema, table) — never both objects.

    Task-9 review: ``_scoped_refs`` matched on the table name ALONE (``parse_ref(r)[2]``), so a bare
    ``subset='accounts'`` matched two distinct objects across two schemas, over-reporting blockers.
    """
    sales = normalize_ref(_SOURCE, "sales", "accounts", "balance")
    risk = normalize_ref(_SOURCE, "risk", "accounts", "balance")
    for ref in (sales, risk):
        _seed(
            db, ref, "additivity", "semi_additive",
            EvidenceProducer.TAXONOMY, AssertionStrength.PROPOSED,
        )
    resolve_and_project(db, source=_SOURCE, logical_refs=[sales, risk])

    rep = compute_readiness(
        db, source=_SOURCE, scope=ReadinessScopeType.TABLE, subset="sales.accounts"
    )
    ids = {r.requirement_id for r in rep.blocking_requirements} | {
        r.requirement_id for r in rep.review_requirements
    }
    assert ids, "expected requirements for the in-scope (sales.accounts) object"
    # covers the sales.accounts object (fields AND its structural facts)...
    assert any("sales.accounts" in i for i in ids)
    assert any(i.startswith("grain:") and "sales.accounts" in i for i in ids)
    assert any(i.endswith(":additivity") and "sales.accounts" in i for i in ids)
    # ...never the same-named table in the OTHER (risk) schema.
    assert not any("risk" in i for i in ids)

    # A BARE table name is ambiguous across the two schemas -> a clear error, never a silent
    # over-match of both objects.
    with pytest.raises(ValueError, match="ambiguous"):
        compute_readiness(
            db, source=_SOURCE, scope=ReadinessScopeType.TABLE, subset="accounts"
        )


def test_unknown_subset_surfaces_not_ready(resolved):
    """A misspelled / unknown TABLE subset must NOT read as a clean 'ready' — it surfaces a
    ``subset_not_found`` blocker (Task-9 review: an empty ref set emitted zero requirements, so a
    typo was indistinguishable from a genuinely clean table)."""
    db, _balance, _region = resolved
    rep = compute_readiness(
        db, source=_SOURCE, scope=ReadinessScopeType.TABLE, subset="accountz"  # typo
    )
    assert rep.operational_status == "blocked"
    assert rep.summary_scores["ready_fraction"] != 1.0
    causes = {r.cause for r in rep.blocking_requirements}
    assert "subset_not_found" in causes
    assert not rep.review_requirements


def test_empty_explicit_subset_is_not_ready(resolved):
    """An explicit but empty logical_ref subset (``subset=[]``) matches nothing — also surfaced as
    ``subset_not_found``, never a false 'ready'."""
    db, _balance, _region = resolved
    rep = compute_readiness(
        db, source=_SOURCE, scope=ReadinessScopeType.TABLE, subset=[]
    )
    assert rep.operational_status == "blocked"
    assert any(r.cause == "subset_not_found" for r in rep.blocking_requirements)


def test_empty_source_is_ready_with_no_requirements(db):
    # A source with nothing resolved yet is trivially "ready" (no blocking requirements) — the gate
    # is blocker-based, so an empty requirement list is not a failure.
    rep = compute_readiness(db, source="nonexistent", scope=ReadinessScopeType.CATALOG)
    assert rep.operational_status == "ready"
    assert rep.blocking_requirements == ()
    assert rep.review_requirements == ()
    assert rep.advisory_gaps == ()
    assert rep.summary_scores["ready_fraction"] == 1.0
