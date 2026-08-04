"""Release C Task 10 — crosswalk candidate discovery.

The truth rules, as tests:

* `data_role=crosswalk` ALONE never creates a definition (§4-correction-10) — the role plus an
  identifier shape across two NAMESPACES proposes a candidate for review, and nothing else does;
* every candidate is a PROPOSED definition, and nothing anywhere is executable;
* an LLM suggestion is INGESTED through the existing structured-result path and validated against
  the same registry/namespace rules — no LLM call is made here;
* transformed and semantic-only suggestions are discoverable and structurally non-executable;
* enumeration is bounded BEFORE any pairwise expansion, and every cap that bites is reported.
"""
from __future__ import annotations

import pytest
from tests.featuregen.overlay.upload._crosswalk_fixtures import (
    CIB_KEY,
    CIB_TABLE,
    FTR_KEY,
    MAP,
    MAP_CIB,
    MAP_FTR,
    build_catalog,
)

from featuregen.overlay.upload.crosswalk_discovery import (
    CROSSWALK_SUGGESTION_RESULT_TYPE,
    MAX_ENDPOINT_COLUMNS_PER_NAMESPACE,
    REASON_SINGLE_NAMESPACE,
    REASON_SUGGESTION_MALFORMED,
    REASON_SUGGESTION_NAMESPACE_MISMATCH,
    REASON_SUGGESTION_SAME_NAMESPACE,
    REASON_SUGGESTION_UNREADABLE_REF,
    REASON_TOO_FEW_IDENTIFIERS,
    SUBJECT_DATASET,
    TRUNCATION_ENDPOINT_COLUMNS,
    discover_crosswalk_candidates,
    persist_crosswalk_candidates,
)
from featuregen.overlay.upload.crosswalk_store import visible_crosswalk_definitions
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.semantic_context import RelationshipKind
from featuregen.overlay.upload.structured_results import (
    record_structured_result,
    set_current_structured_result,
)

ACTOR = "user:priya"


def _suggest(db, *, subject_ref: str = MAP, crosswalks: list[dict], version: int = 1) -> str:
    """Record a crosswalk suggestion through the EXISTING structured-result path. NO LLM CALL —
    this is exactly what the joint enrichment surface will write when it starts issuing one."""
    result = record_structured_result(
        db, result_type=CROSSWALK_SUGGESTION_RESULT_TYPE, result_version=version,
        input_content_hash=f"input-{subject_ref}-{version}",
        output={"crosswalks": crosswalks},
        producer_kind="llm", producer_ref="claude/crosswalk-suggest@v1")
    set_current_structured_result(
        db, subject_kind=SUBJECT_DATASET, subject_ref=subject_ref,
        result_type=CROSSWALK_SUGGESTION_RESULT_TYPE,
        structured_result_id=result.structured_result_id)
    return result.structured_result_id


def _good_suggestion() -> dict:
    return {"source_column_ref": CIB_KEY, "mapping_source_column_ref": MAP_CIB,
            "mapping_target_column_ref": MAP_FTR, "target_column_ref": FTR_KEY}


# ── source (a): role + identifier shape ─────────────────────────────────────────────────────────

def test_a_crosswalk_role_plus_two_namespaces_proposes_a_candidate(db) -> None:
    build_catalog(db)
    report = discover_crosswalk_candidates(db)
    assert report.mapping_datasets_considered == 1
    assert len(report.candidates) == 1
    candidate = report.candidates[0]
    assert candidate.relationship_kind is RelationshipKind.CROSSWALK
    assert candidate.origin == "dataset_role"
    assert candidate.mapping_dataset_ref == MAP
    assert {candidate.source_namespace, candidate.target_namespace} == {
        "internal_account", "external_account"}
    assert candidate.definition is not None
    assert candidate.executable is False


def test_the_role_ALONE_never_creates_a_definition(db) -> None:
    """§4-correction-10, the rule this whole task hangs on. A table labelled `bridge` whose columns
    do not carry two identifier namespaces proposes NOTHING — and the refusal is a counted reason,
    not a silent drop."""
    build_catalog(db, map_columns=(("mapping_note", "party_name", ""),
                                   ("valid_from", "effective_from", "")))
    report = discover_crosswalk_candidates(db)
    assert report.mapping_datasets_considered == 1
    assert report.candidates == ()
    assert report.reason_counts[REASON_TOO_FEW_IDENTIFIERS] == 1


def test_two_identifiers_in_ONE_namespace_is_a_duplicate_key_not_a_mapping(db) -> None:
    build_catalog(db, map_columns=(("acct_no", "account_id", ""),
                                   ("alt_acct_no", "account_id", "")))
    report = discover_crosswalk_candidates(db)
    assert report.candidates == ()
    assert report.reason_counts[REASON_SINGLE_NAMESPACE] == 1


def test_a_table_with_no_crosswalk_role_is_never_a_mapping_dataset(db) -> None:
    """The identifier shape ALONE is not enough either: both halves are required."""
    build_catalog(db, map_table_role=None)
    report = discover_crosswalk_candidates(db)
    assert report.mapping_datasets_considered == 0
    assert report.candidates == ()


def test_the_input_alias_crosswalk_is_accepted_beside_the_canonical_bridge(db) -> None:
    build_catalog(db, map_table_role="crosswalk")
    report = discover_crosswalk_candidates(db)
    assert report.mapping_datasets_considered == 1
    assert report.candidates


def test_the_mapping_table_is_never_its_own_endpoint(db) -> None:
    build_catalog(db)
    report = discover_crosswalk_candidates(db)
    for candidate in report.candidates:
        assert MAP not in {candidate.definition.source_endpoint.logical_table_ref,
                           candidate.definition.target_endpoint.logical_table_ref}


# ── candidates are PROPOSED, and nothing is executable ──────────────────────────────────────────

def test_every_candidate_persists_as_an_unreviewed_definition_with_evidence(db) -> None:
    build_catalog(db)
    report = discover_crosswalk_candidates(db)
    published = persist_crosswalk_candidates(db, report, actor=ACTOR)
    assert published
    stored = visible_crosswalk_definitions(db)
    assert {c.current.revision_id for c in stored} == set(published)
    for crosswalk in stored:
        assert crosswalk.review_status.value == "unreviewed"
        assert crosswalk.revision.evidence_refs, "a candidate without evidence explains nothing"
        # STRUCTURAL evidence names BOTH halves of the rule: the role, and each namespace.
        ids = {ref.evidence_id for ref in crosswalk.revision.evidence_refs}
        assert any(i.startswith("crosswalk_role:") for i in ids)
        assert sum(i.startswith("crosswalk_namespace:") for i in ids) == 2


def test_persisting_twice_is_idempotent(db) -> None:
    build_catalog(db)
    report = discover_crosswalk_candidates(db)
    first = persist_crosswalk_candidates(db, report, actor=ACTOR)
    second = persist_crosswalk_candidates(db, report, actor=ACTOR)
    assert first == second
    versions = {c.current.pointer_version for c in visible_crosswalk_definitions(db)}
    assert versions == {1}


def test_discovery_writes_nothing_by_itself(db) -> None:
    """A read-only review of what WOULD be proposed must not mutate the catalog."""
    build_catalog(db)
    discover_crosswalk_candidates(db)
    assert db.execute("SELECT count(*) FROM crosswalk_definition_revision").fetchone()[0] == 0


def test_discovery_produces_no_plan_and_imports_no_planner() -> None:
    """Structural, not aspirational: the ONE permitted `materialize` import across the crosswalk
    modules is the D1 hasher. A planner, renderer or join import would BE the execution path."""
    import ast
    import pathlib

    import featuregen.overlay.upload.crosswalk_discovery as module
    import featuregen.overlay.upload.crosswalk_store as store

    imported: set[str] = set()
    for target in (module, store):
        tree = ast.parse(pathlib.Path(target.__file__).read_text())
        imported |= {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("featuregen.materialize")}
    assert imported <= {"featuregen.materialize.canonical"}


# ── source (b): the LLM ingestion seam (no LLM call) ────────────────────────────────────────────

def test_a_structured_suggestion_becomes_a_candidate_with_llm_evidence(db) -> None:
    build_catalog(db, map_table_role="bridge")
    result_id = _suggest(db, crosswalks=[_good_suggestion()])
    report = discover_crosswalk_candidates(db)
    llm = [c for c in report.candidates if c.origin == "llm_suggestion"]
    assert len(llm) == 1
    assert llm[0].definition is not None
    assert [ref.evidence_id for ref in llm[0].evidence_refs] == [result_id]
    assert [ref.kind.value for ref in llm[0].evidence_refs] == ["llm_recommendation"]


def test_a_suggestion_and_the_structural_rule_agree_on_ONE_crosswalk(db) -> None:
    """Both sources may propose the same crosswalk; the pass must not emit it twice."""
    build_catalog(db)
    _suggest(db, crosswalks=[_good_suggestion()])
    report = discover_crosswalk_candidates(db)
    revision_ids = [c.definition.revision_id for c in report.candidates if c.definition]
    assert len(revision_ids) == len(set(revision_ids))


@pytest.mark.parametrize("mutation,reason", [
    ({"source_column_ref": normalize_ref("cib", "public", CIB_TABLE, "ghost")},
     REASON_SUGGESTION_UNREADABLE_REF),
    ({"target_column_ref": CIB_KEY}, REASON_SUGGESTION_SAME_NAMESPACE),
    ({"mapping_target_column_ref": MAP_CIB}, REASON_SUGGESTION_NAMESPACE_MISMATCH),
])
def test_an_ungrounded_suggestion_is_refused_with_a_counted_reason(db, mutation, reason) -> None:
    """An LLM may not propose a relationship the deterministic path would have refused — and a
    refusal is COUNTED, because a suggestion that quietly vanished is worse than a wrong one."""
    build_catalog(db)
    suggestion = _good_suggestion() | mutation
    _suggest(db, crosswalks=[suggestion])
    report = discover_crosswalk_candidates(db)
    assert report.reason_counts.get(reason) == 1
    assert not [c for c in report.candidates
                if c.origin == "llm_suggestion" and c.definition is not None
                and c.definition.source_ref == suggestion["source_column_ref"]
                and c.definition.target_ref == suggestion["target_column_ref"]]


def test_a_malformed_suggestion_never_ends_the_pass(db) -> None:
    build_catalog(db)
    _suggest(db, crosswalks=[{"source_column_ref": CIB_KEY}, _good_suggestion()])
    report = discover_crosswalk_candidates(db)
    assert report.reason_counts.get(REASON_SUGGESTION_MALFORMED) == 1
    assert [c for c in report.candidates if c.origin == "llm_suggestion"]


def test_a_suggestion_naming_a_restricted_column_is_invisible_to_an_unprivileged_caller(db) -> None:
    """The read-scope answer must not become an existence oracle: the unreadable and the
    nonexistent produce the SAME reason code."""
    build_catalog(db, ftr_columns=(("counter_party_acct_no", "external_account_ref", "restricted"),
                                   ("party_lei", "lei", "")))
    _suggest(db, crosswalks=[_good_suggestion()])
    unprivileged = discover_crosswalk_candidates(db, roles=())
    assert unprivileged.reason_counts.get(REASON_SUGGESTION_UNREADABLE_REF) == 1
    privileged = discover_crosswalk_candidates(db, roles=("restricted_reader",))
    assert [c for c in privileged.candidates if c.origin == "llm_suggestion"]


@pytest.mark.parametrize("kind", ["transformed", "semantic_only"])
def test_transformed_and_semantic_only_suggestions_are_discoverable_and_undefinable(db, kind) -> None:
    """Discoverable: returned by the pass, with their kind intact. Structurally non-executable:
    NO definition is built, because a crosswalk definition asserts equality through a mapping table
    and this repo has no transformation contract (§4-correction-9)."""
    build_catalog(db)
    _suggest(db, crosswalks=[_good_suggestion() | {"relationship_kind": kind}])
    report = discover_crosswalk_candidates(db)
    proposed = [c for c in report.candidates if c.relationship_kind.value == kind]
    assert len(proposed) == 1
    assert proposed[0].definition is None
    assert proposed[0].executable is False
    published = persist_crosswalk_candidates(db, report, actor=ACTOR)
    stored = {c.revision.source_ref for c in visible_crosswalk_definitions(db)}
    assert len(published) == len(stored) == len(
        [c for c in report.candidates if c.definition is not None])


def test_a_suggestion_for_a_dataset_with_no_crosswalk_role_is_not_read(db) -> None:
    """The suggestion seam hangs off the mapping-dataset pass; it is not a second entry point that
    bypasses the role gate."""
    build_catalog(db, map_table_role=None)
    _suggest(db, crosswalks=[_good_suggestion()])
    report = discover_crosswalk_candidates(db)
    assert report.candidates == ()


# ── bounds ──────────────────────────────────────────────────────────────────────────────────────

def test_endpoints_are_bounded_before_any_pair_is_formed_and_truncation_is_reported(db) -> None:
    wide = tuple((f"acct_no_{i}", "account_id", "") for i in range(
        MAX_ENDPOINT_COLUMNS_PER_NAMESPACE + 5))
    build_catalog(db, cib_columns=wide)
    report = discover_crosswalk_candidates(db)
    assert report.truncated
    assert report.truncation_counts[TRUNCATION_ENDPOINT_COLUMNS] >= 5
    # The cap is on the POOL, so the pair count cannot exceed it by construction.
    assert len(report.candidates) <= MAX_ENDPOINT_COLUMNS_PER_NAMESPACE


def test_a_clean_pass_reports_no_truncation(db) -> None:
    build_catalog(db)
    report = discover_crosswalk_candidates(db)
    assert report.truncated is False
    assert report.truncation_counts == {}
