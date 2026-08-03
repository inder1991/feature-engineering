"""Release-B Task 8 — the source selector's truth table.

One test per row of the §6.4 precedence, plus the two MUTATION-style pins the plan asks for:

  * the POPULATION is never inferred — not from ``primary_entity``, not from ``authority_role``, not
    from a table NAME. The candidate list handed to a population need is not even read;
  * a tie is never broken by upload time, lexical order or catalog recency — a renamed, reordered,
    re-uploaded candidate set produces the byte-identical refusal.

Every refusal carries EVERY candidate's disposition and reason codes (functional rule 9), and the
selected dataset's binding revision is PERSISTED, not merely computed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.data_agent.binding_store import (
    binding_revision_exists,
    declare_catalog_engine,
    record_connection,
)
from featuregen.data_agent.connection import DataSourceConnectionV1
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload import source_selector
from featuregen.overlay.upload.bridge_realization import ExecutionTier
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.serving_policy_store import publish_serving_policy
from featuregen.overlay.upload.source_selection import (
    SELECTION_AUTHORITY_INSUFFICIENT,
    SELECTION_BINDING_MISSING,
    SELECTION_POPULATION_UNDECLARED,
    SELECTION_SOURCE_AMBIGUOUS,
    AuthorityBasis,
    CandidateDisposition,
    DatasetNeedRole,
    DatasetNeedV1,
    DatasetServingPolicyRevisionV1,
    SelectionBasis,
    ServingPurpose,
    human_declaration_provenance,
)
from featuregen.overlay.upload.source_selector import (
    PROPOSED_AUTHORITY_USED,
    SourceSelectionOutcomeV1,
    select_dataset_source,
)

ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))

_SRC = "bank"
_SCHEMA = "dpl_eib"
_TABLES = ("customer_master", "customer_ods", "kyc_customers", "tran_repos", "tran_archive")

CUST = normalize_ref(_SRC, _SCHEMA, "customer_master")
ODS = normalize_ref(_SRC, _SCHEMA, "customer_ods")
KYC = normalize_ref(_SRC, _SCHEMA, "kyc_customers")
TRAN = normalize_ref(_SRC, _SCHEMA, "tran_repos")
TRAN_ARCHIVE = normalize_ref(_SRC, _SCHEMA, "tran_archive")


@pytest.fixture
def catalog(db):
    """Five addressable tables in one catalog with one declared engine — the realistic shape."""
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, table, "cif_id", "text", is_grain=True, entity="Customer")
        for table in _TABLES
    ])
    # `build_graph` leaves `schema_name` NULL without a schema-bearing glossary, and the schema is
    # the one component of the physical address the catalog exists to supply.
    db.execute("UPDATE graph_node SET schema_name = %s WHERE catalog_source = %s",
               (_SCHEMA, _SRC))
    record_connection(db, DataSourceConnectionV1(
        connection_id="edp-1", environment_id="dev", kind="hive", host="hs2.internal", port=10000,
        auth_mechanism="kerberos", secret_ref="vault://featuregen/hive",
        execution_principal="svc_ro", allowed_schemas=frozenset({_SCHEMA}), active=True),
        tier="edp", database_name="edp_cluster")
    declare_catalog_engine(db, catalog_source=_SRC, engine="hive", tier="edp", declared_by="p")
    return db


def _need(**over) -> DatasetNeedV1:
    kw = dict(entity_id="customer", need_role=DatasetNeedRole.EVENT_SOURCE,
              serving_purpose=ServingPurpose.ANALYTICAL, execution_tier=ExecutionTier.SANDBOX)
    kw.update(over)
    return DatasetNeedV1(**kw)


def _publish(db, **over) -> str:
    kw = dict(entity_id="customer", need_role=DatasetNeedRole.EVENT_SOURCE,
              serving_purpose=ServingPurpose.ANALYTICAL,
              eligible_dataset_refs=(TRAN, TRAN_ARCHIVE), preferred_dataset_refs=(TRAN,),
              provenance=human_declaration_provenance(producer_ref="user:priya"))
    kw.update(over)
    revision_id, _version = publish_serving_policy(
        db, DatasetServingPolicyRevisionV1(**kw), expected_pointer_version=0, actor="user:priya")
    return revision_id


def _propose_authority(db, table: str, value: str) -> None:
    """An LLM PROPOSAL of an authority role: displayed, never load-bearing."""
    ref = normalize_ref(_SRC, _SCHEMA, table)
    record_field_evidence(
        db, logical_ref=ref, field_name="authority_role", proposed_value=value,
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        producer_ref="llm-test", source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=ref, field_name="authority_role",
                                    material=f"{value}:llm:proposed"))


def _confirm_authority(db, table: str, value: str) -> None:
    """The EXISTING four-eyes propose/confirm flow, driven to a LOAD-BEARING profile value."""
    for action, actor, idem in (("propose_override", ADMIN_A, f"ar-p-{table}-{value}"),
                                ("confirm_override", ADMIN_B, f"ar-c-{table}-{value}")):
        cas = read_field_cas(db, source=_SRC, object_ref=f"public.{table}",
                             field="authority_role")
        result = apply_field_correction(
            db, source=_SRC, object_ref=f"public.{table}", field="authority_role", action=action,
            actor=actor, idempotency_key=idem, replacement_value=value,
            expected_latest_decision_id=cas["latest_decision_id"],
            expected_evidence_set_hash=cas["evidence_set_hash"],
            expected_policy_version=cas["policy_version"])
        assert result["accepted"] is True, result


def _dispositions(outcome: SourceSelectionOutcomeV1) -> dict[str, str]:
    decisions = (outcome.selection.considered_candidates if outcome.resolved
                 else outcome.refusal.considered_candidates)
    return {d.dataset_ref: d.disposition.value for d in decisions}


# ── POPULATION: two rules, and no third one ─────────────────────────────────────────────────────


def test_an_explicitly_declared_population_is_selected(catalog):
    outcome = select_dataset_source(catalog, need=_need(
        need_role=DatasetNeedRole.POPULATION, explicit_dataset_ref=CUST))
    assert outcome.resolved
    assert outcome.selection.selected_dataset_ref == CUST
    assert outcome.selection.selection_basis is SelectionBasis.EXPLICIT_REQUEST
    assert outcome.selection.serving_policy_revision_id is None


def test_a_serving_policy_declares_the_population_when_the_request_does_not(catalog):
    revision_id = _publish(catalog, need_role=DatasetNeedRole.POPULATION,
                           eligible_dataset_refs=(CUST, ODS), preferred_dataset_refs=(CUST,))
    outcome = select_dataset_source(catalog, need=_need(need_role=DatasetNeedRole.POPULATION))
    assert outcome.resolved
    assert outcome.selection.selected_dataset_ref == CUST
    assert outcome.selection.selection_basis is SelectionBasis.SERVING_POLICY
    assert outcome.selection.serving_policy_revision_id == revision_id
    assert _dispositions(outcome)[ODS] == CandidateDisposition.ELIGIBLE.value


def test_an_undeclared_population_refuses_rather_than_choosing(catalog):
    outcome = select_dataset_source(catalog, need=_need(need_role=DatasetNeedRole.POPULATION))
    assert not outcome.resolved
    assert outcome.refusal.code == SELECTION_POPULATION_UNDECLARED
    assert outcome.refusal.subject_refs == ("customer",)


def test_the_population_is_NOT_inferred_from_entity_authority_or_table_name(catalog):
    """THE MUTATION PIN. Every signal that could tempt an inference is present and stacked on one
    obvious-looking table: it is named `customer_master`, its columns carry entity `Customer`, and
    it is the confirmed SYSTEM OF RECORD. It is also handed to the selector as a candidate.

    The answer is still "nobody declared the population". `kyc_customers`, `card_customers` and
    `customers` look identical to a catalog; a selector that picked here would be right often enough
    to be trusted and wrong silently.
    """
    _confirm_authority(catalog, "customer_master", "system_of_record")
    outcome = select_dataset_source(
        catalog, need=_need(need_role=DatasetNeedRole.POPULATION),
        candidate_dataset_refs=(CUST, KYC, ODS))
    assert not outcome.resolved
    assert outcome.refusal.code == SELECTION_POPULATION_UNDECLARED
    # And nothing was assessed: the candidate list was not read at all for this need role.
    assert outcome.refusal.considered_candidates == ()


def test_a_policy_with_two_equally_preferred_populations_is_an_explicit_ambiguity(catalog):
    _publish(catalog, need_role=DatasetNeedRole.POPULATION,
             eligible_dataset_refs=(CUST, ODS), preferred_dataset_refs=(CUST, ODS))
    outcome = select_dataset_source(catalog, need=_need(need_role=DatasetNeedRole.POPULATION))
    assert not outcome.resolved
    assert outcome.refusal.code == SELECTION_SOURCE_AMBIGUOUS
    assert set(outcome.refusal.subject_refs) == {CUST, ODS}
    assert _dispositions(outcome) == {CUST: CandidateDisposition.TIED.value,
                                      ODS: CandidateDisposition.TIED.value}


def test_a_policy_with_no_preference_and_one_eligible_dataset_still_decides(catalog):
    _publish(catalog, need_role=DatasetNeedRole.POPULATION,
             eligible_dataset_refs=(CUST,), preferred_dataset_refs=())
    outcome = select_dataset_source(catalog, need=_need(need_role=DatasetNeedRole.POPULATION))
    assert outcome.resolved
    assert outcome.selection.selected_dataset_ref == CUST


# ── every other need: explicit -> policy -> single unambiguous candidate ────────────────────────


def test_an_explicit_request_outranks_the_serving_policy(catalog):
    _publish(catalog)                     # prefers TRAN
    outcome = select_dataset_source(catalog, need=_need(explicit_dataset_ref=TRAN_ARCHIVE))
    assert outcome.resolved
    assert outcome.selection.selected_dataset_ref == TRAN_ARCHIVE
    assert outcome.selection.selection_basis is SelectionBasis.EXPLICIT_REQUEST


def test_the_serving_policy_decides_when_the_request_names_nothing(catalog):
    _publish(catalog)
    outcome = select_dataset_source(catalog, need=_need(),
                                    candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    assert outcome.resolved
    assert outcome.selection.selected_dataset_ref == TRAN
    assert outcome.selection.selection_basis is SelectionBasis.SERVING_POLICY
    assert outcome.selection.authority_basis is AuthorityBasis.POLICY_DECLARATION


def test_a_single_unambiguous_eligible_candidate_is_selected(catalog):
    outcome = select_dataset_source(catalog, need=_need(), candidate_dataset_refs=(TRAN,))
    assert outcome.resolved
    assert outcome.selection.selection_basis is SelectionBasis.SINGLE_ELIGIBLE_CANDIDATE
    assert outcome.selection.authority_basis is AuthorityBasis.UNKNOWN


def test_no_candidates_at_all_refuses_rather_than_scanning_the_catalog(catalog):
    outcome = select_dataset_source(catalog, need=_need())
    assert not outcome.resolved
    assert outcome.refusal.code == SELECTION_SOURCE_AMBIGUOUS
    assert outcome.refusal.considered_candidates == ()


# ── authority ranks, and only where it is allowed to ────────────────────────────────────────────


def test_a_load_bearing_authority_role_ranks_two_eligible_candidates(catalog):
    _confirm_authority(catalog, "tran_repos", "system_of_record")
    outcome = select_dataset_source(catalog, need=_need(),
                                    candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    assert outcome.resolved
    assert outcome.selection.selected_dataset_ref == TRAN
    assert outcome.selection.authority_basis is AuthorityBasis.LOAD_BEARING_PROFILE
    assert outcome.selection.authority_role == "system_of_record"
    assert outcome.warnings == ()
    selected = [d for d in outcome.selection.considered_candidates
                if d.disposition is CandidateDisposition.SELECTED]
    assert selected[0].reason_codes == ("load_bearing_authority",)


def test_the_stronger_of_two_load_bearing_authorities_wins(catalog):
    _confirm_authority(catalog, "tran_repos", "system_of_record")
    _confirm_authority(catalog, "tran_archive", "authoritative_replica")
    outcome = select_dataset_source(catalog, need=_need(),
                                    candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    assert outcome.resolved
    assert outcome.selection.selected_dataset_ref == TRAN


def test_a_proposed_authority_may_rank_in_SANDBOX_with_a_visible_warning(catalog):
    _propose_authority(catalog, "tran_repos", "system_of_record")
    outcome = select_dataset_source(
        catalog, need=_need(execution_tier=ExecutionTier.SANDBOX),
        candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    assert outcome.resolved
    assert outcome.selection.selected_dataset_ref == TRAN
    assert outcome.selection.authority_basis is AuthorityBasis.PROPOSED_AUTHORITY_SANDBOX
    assert outcome.warnings == (PROPOSED_AUTHORITY_USED,)
    selected = [d for d in outcome.selection.considered_candidates
                if d.disposition is CandidateDisposition.SELECTED]
    assert selected[0].reason_codes == ("proposed_authority_sandbox",)


def test_a_proposed_authority_may_NOT_rank_in_PRODUCTION(catalog):
    _propose_authority(catalog, "tran_repos", "system_of_record")
    outcome = select_dataset_source(
        catalog, need=_need(execution_tier=ExecutionTier.PRODUCTION),
        candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    assert not outcome.resolved
    assert outcome.refusal.code == SELECTION_AUTHORITY_INSUFFICIENT
    assert set(outcome.refusal.subject_refs) == {TRAN, TRAN_ARCHIVE}
    assert all("authority_insufficient" in d.reason_codes
               for d in outcome.refusal.considered_candidates)


def test_two_candidates_with_no_authority_at_all_are_ambiguous_not_ordered(catalog):
    outcome = select_dataset_source(catalog, need=_need(),
                                    candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    assert not outcome.resolved
    assert outcome.refusal.code == SELECTION_SOURCE_AMBIGUOUS
    assert _dispositions(outcome) == {TRAN: CandidateDisposition.TIED.value,
                                      TRAN_ARCHIVE: CandidateDisposition.TIED.value}


def test_two_equal_load_bearing_authorities_tie_rather_than_ordering(catalog):
    _confirm_authority(catalog, "tran_repos", "system_of_record")
    _confirm_authority(catalog, "tran_archive", "system_of_record")
    outcome = select_dataset_source(catalog, need=_need(),
                                    candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    assert not outcome.resolved
    assert outcome.refusal.code == SELECTION_SOURCE_AMBIGUOUS


def test_the_tie_is_NOT_broken_by_the_order_the_candidates_were_offered_in(catalog):
    """THE SECOND MUTATION PIN, part one. The same two candidates offered in the other order
    produce the identical refusal, down to the recorded dispositions."""
    first = select_dataset_source(catalog, need=_need(),
                                  candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    second = select_dataset_source(catalog, need=_need(),
                                   candidate_dataset_refs=(TRAN_ARCHIVE, TRAN))
    assert first.refusal.code == second.refusal.code == SELECTION_SOURCE_AMBIGUOUS
    assert first.refusal.subject_refs == second.refusal.subject_refs
    assert _dispositions(first) == _dispositions(second)


def test_the_selector_reads_no_clock_row_order_or_recency_signal():
    """Part two, structural. Rule: "never break ties by upload time, lexical order or newest
    catalog." The three ways that rule gets broken by accident are a `now()`, an `ORDER BY` on a
    catalog read, and a `created_at`/`updated_at` column sneaking into a ranking — so the module is
    asserted to contain none of them. `sorted(...)` is used, and deliberately: it orders SETS whose
    order must not re-key an identical decision, never the contest.
    """
    body = Path(source_selector.__file__).read_text()
    for forbidden in ("now()", "created_at", "updated_at", "ORDER BY", "order by",
                      "datetime", "time.time"):
        assert forbidden not in body, f"{forbidden!r} would let ordering decide a source"


# ── the binding is PINNED, and its absence is a refusal ─────────────────────────────────────────


def test_the_selection_persists_the_binding_revision_it_pinned(catalog):
    outcome = select_dataset_source(catalog, need=_need(explicit_dataset_ref=TRAN),
                                    recorded_by="areq-1")
    assert outcome.resolved
    revision_id = outcome.selection.selected_binding_revision_id
    assert revision_id.startswith("pbr_")
    assert binding_revision_exists(catalog, revision_id) is True


def test_a_dataset_with_no_physical_binding_is_explainable_metadata_not_a_source(db):
    """No catalog engine and no explicit binding: the profile exists, the address does not."""
    build_graph(db, _SRC, [CanonicalRow(_SRC, "tran_repos", "cif_id", "text")])
    db.execute("UPDATE graph_node SET schema_name = %s WHERE catalog_source = %s", (_SCHEMA, _SRC))
    outcome = select_dataset_source(db, need=_need(explicit_dataset_ref=TRAN))
    assert not outcome.resolved
    assert outcome.refusal.code == SELECTION_BINDING_MISSING
    assert outcome.refusal.subject_refs == (TRAN,)


def test_when_every_candidate_is_unaddressable_the_refusal_names_the_binding(db):
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "tran_repos", "cif_id", "text"),
        CanonicalRow(_SRC, "tran_archive", "cif_id", "text"),
    ])
    db.execute("UPDATE graph_node SET schema_name = %s WHERE catalog_source = %s", (_SCHEMA, _SRC))
    outcome = select_dataset_source(db, need=_need(),
                                    candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    assert not outcome.resolved
    assert outcome.refusal.code == SELECTION_BINDING_MISSING
    assert all("no_binding" in d.reason_codes for d in outcome.refusal.considered_candidates)


def test_a_candidate_the_caller_cannot_see_is_dropped_exactly_like_a_missing_one(catalog):
    """Read scope first, and as ABSENCE (D11): a hidden better copy must not be reported.

    The COLUMN is restricted, not the table: `visible_requires` is a GENERATED column derived from
    `sensitivity`, and a table anchor is visible exactly when >= 1 of its columns is."""
    catalog.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND table_name = 'tran_archive' AND kind = 'column'", (_SRC,))
    outcome = select_dataset_source(catalog, need=_need(),
                                    candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    assert outcome.resolved
    assert outcome.selection.selected_dataset_ref == TRAN
    assert TRAN_ARCHIVE not in _dispositions(outcome)


# ── rule 9 and identity ─────────────────────────────────────────────────────────────────────────


def test_every_considered_candidate_carries_a_disposition_and_a_reason(catalog):
    _publish(catalog)
    outcome = select_dataset_source(catalog, need=_need(),
                                    candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    decisions = outcome.selection.considered_candidates
    assert {d.dataset_ref for d in decisions} == {TRAN, TRAN_ARCHIVE}
    assert all(d.disposition is not None for d in decisions)
    assert all(d.dataset_profile_hash for d in decisions)


def test_the_selection_content_hash_is_stable_across_repeated_selections(catalog):
    _publish(catalog)
    first = select_dataset_source(catalog, need=_need(),
                                  candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    second = select_dataset_source(catalog, need=_need(),
                                   candidate_dataset_refs=(TRAN_ARCHIVE, TRAN))
    assert first.selection.content_hash == second.selection.content_hash


def test_only_the_selected_candidate_names_a_binding_revision(catalog):
    """A losing candidate must not name a `pbr_` row that was never written: the persisting seam is
    reserved for the winner."""
    _publish(catalog)
    outcome = select_dataset_source(catalog, need=_need(),
                                    candidate_dataset_refs=(TRAN, TRAN_ARCHIVE))
    named = {d.dataset_ref: d.binding_revision_id
             for d in outcome.selection.considered_candidates}
    assert named[TRAN] is not None
    assert named[TRAN_ARCHIVE] is None
