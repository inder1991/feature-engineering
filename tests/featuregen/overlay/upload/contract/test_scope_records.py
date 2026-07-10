"""Phase-1B Task 2 — scope-record persistence round-trips (real `db` connection).

Covers the recognition -> run -> scope lineage: an idempotent attempt (quintet stamped, proposals
retained), a confirmed scope with normalized child rows (relationship/origin/display_order),
`scope_for_run` reconstruction, the queryable proposed-vs-accepted delta, and the unique canonical
run->scope linkage.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.overlay.upload.contract.scope_records import (
    confirmation_delta,
    record_confirmed_scope,
    record_recognition_attempt,
    scope_for_run,
)
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope, ScopeExpansion
from featuregen.overlay.upload.taxonomy.recognition import (
    RecognitionResult,
    RecognitionStatus,
    UseCaseCandidate,
)

PRIMARY = "retail_churn"
SECONDARY = "credit_default"


def _result() -> RecognitionResult:
    """A CLASSIFIED result carrying one primary + one secondary PROPOSAL, stamped with a distinct
    version quintet so we can assert every column is persisted verbatim."""
    return RecognitionResult(
        status=RecognitionStatus.CLASSIFIED,
        candidates=(
            UseCaseCandidate(use_case_id=PRIMARY, relationship="primary", confidence="high",
                             evidence_spans=("balance dropped sharply",), rationale="clear churn signal"),
            UseCaseCandidate(use_case_id=SECONDARY, relationship="secondary", confidence="medium",
                             evidence_spans=("missed a payment",), rationale="supporting risk signal"),
        ),
        ambiguity_note=None,
        taxonomy_version="tax_v9",
        recognizer_model_id="model_v9",
        prompt_version="prompt_v9",
        applicability_mapping_version="map_v9",
        recipe_registry_version="reg_v9",
    )


def test_recognition_attempt_is_idempotent_and_stamps_quintet(db) -> None:
    rid = record_recognition_attempt(
        db, intent_id="intent_1", input_hash="hash_1", result=_result(), actor="ds1")
    assert rid  # returns an id

    # A SECOND call with the same (intent_id, input_hash) returns the SAME id, not a new row.
    rid_again = record_recognition_attempt(
        db, intent_id="intent_1", input_hash="hash_1", result=_result(), actor="ds1")
    assert rid_again == rid
    count = db.execute(
        "SELECT count(*) FROM intent_recognition_attempt WHERE intent_id = %s AND input_hash = %s",
        ("intent_1", "hash_1")).fetchone()[0]
    assert count == 1  # exactly ONE row despite two calls

    # The stored candidates preserve BOTH proposals, verbatim (proposed half of the delta).
    row = db.execute(
        "SELECT candidates, taxonomy_version, applicability_mapping_version, recognizer_model_id, "
        "prompt_version, recipe_registry_version, status, created_by "
        "FROM intent_recognition_attempt WHERE recognition_id = %s", (rid,)).fetchone()
    candidates, tax, mapv, model, prompt, reg, status, created_by = row
    assert [c["use_case_id"] for c in candidates] == [PRIMARY, SECONDARY]
    assert candidates[0]["relationship"] == "primary"
    assert candidates[0]["evidence_spans"] == ["balance dropped sharply"]
    assert candidates[1]["relationship"] == "secondary"
    # The version quintet columns are stamped from the result.
    assert (tax, mapv, model, prompt, reg) == (
        "tax_v9", "map_v9", "model_v9", "prompt_v9", "reg_v9")
    assert status == "classified"
    assert created_by == {"subject": "ds1"}


def test_recognition_attempt_round_trips_dimensions(db) -> None:
    # The optional confirmed-intent dimensions (modelling_contexts / target_entity) + the per-dimension
    # warnings persist verbatim on the attempt alongside the version quintet.
    result = RecognitionResult(
        status=RecognitionStatus.CLASSIFIED,
        candidates=(UseCaseCandidate(use_case_id=PRIMARY, relationship="primary", confidence="high",
                                     evidence_spans=("balance dropped",), rationale="churn signal"),),
        ambiguity_note=None,
        taxonomy_version="tax_v9", recognizer_model_id="model_v9", prompt_version="prompt_v9",
        applicability_mapping_version="map_v9", recipe_registry_version="reg_v9",
        modelling_contexts=("ifrs9", "frtb"),
        target_entity="customer",
        warnings=("UNKNOWN_TARGET_ENTITY",),
    )
    rid = record_recognition_attempt(
        db, intent_id="intent_dim", input_hash="hash_dim", result=result, actor="ds1")

    row = db.execute(
        "SELECT modelling_contexts, target_entity, warnings FROM intent_recognition_attempt "
        "WHERE recognition_id = %s", (rid,)).fetchone()
    contexts, entity, warnings = row
    assert contexts == ["ifrs9", "frtb"]
    assert entity == "customer"
    assert warnings == ["UNKNOWN_TARGET_ENTITY"]


def test_recognition_attempt_defaults_empty_dimensions(db) -> None:
    # A result with no confirmed dimensions persists the additive columns at their empty defaults.
    rid = record_recognition_attempt(
        db, intent_id="intent_nodim", input_hash="hash_nodim", result=_result(), actor="ds1")
    row = db.execute(
        "SELECT modelling_contexts, target_entity, warnings FROM intent_recognition_attempt "
        "WHERE recognition_id = %s", (rid,)).fetchone()
    assert row == ([], None, [])


def test_confirmed_scope_writes_children_and_round_trips(db) -> None:
    rid = record_recognition_attempt(
        db, intent_id="intent_1", input_hash="hash_1", result=_result(), actor="ds1")
    scope = ConfirmedScope(primary=PRIMARY, secondary=(SECONDARY,),
                           expansion=ScopeExpansion.EXACT, unscoped=False)
    scope_id = record_confirmed_scope(
        db, intent_id="intent_1", generation_run_id="run_1", recognition_id=rid, scope=scope,
        use_case_origins={SECONDARY: "user_added"},
        confirmation_source="user_confirmed", confirmed_by="ds1")
    assert scope_id

    children = db.execute(
        "SELECT use_case_id, relationship, origin, display_order FROM confirmed_scope_use_case "
        "WHERE scope_id = %s ORDER BY display_order", (scope_id,)).fetchall()
    assert children == [
        (PRIMARY, "primary", "llm_proposed", 0),     # primary defaults to llm_proposed, order 0
        (SECONDARY, "secondary", "user_added", 1),   # secondary origin overridden, order 1
    ]

    # scope_for_run reconstructs the EXACT ConfirmedScope by run id only (canonical linkage).
    reconstructed = scope_for_run(db, "run_1")
    assert reconstructed == scope


def test_unscoped_scope_has_no_children_and_round_trips(db) -> None:
    scope = ConfirmedScope(primary=None, unscoped=True)
    scope_id = record_confirmed_scope(
        db, intent_id="intent_2", generation_run_id="run_2", recognition_id=None, scope=scope,
        use_case_origins={}, confirmation_source="user_broadened", confirmed_by="ds1")

    mode = db.execute(
        "SELECT scope_mode FROM confirmed_generation_scope WHERE scope_id = %s",
        (scope_id,)).fetchone()[0]
    assert mode == "unscoped"
    n_children = db.execute(
        "SELECT count(*) FROM confirmed_scope_use_case WHERE scope_id = %s", (scope_id,)).fetchone()[0]
    assert n_children == 0  # an unscoped scope has no primary/secondary -> no child rows

    reconstructed = scope_for_run(db, "run_2")
    assert reconstructed == ConfirmedScope(primary=None, secondary=(), unscoped=True)
    assert reconstructed.primary is None and reconstructed.unscoped is True


def test_unscoped_scope_ignores_stray_primary_and_writes_no_children(db) -> None:
    # A defensive guard (Fix 4): an unscoped scope that inconsistently still carries a primary/secondary
    # must write ZERO child rows — the child-insert loop is skipped when scope.unscoped, so the persisted
    # rows stay consistent with scope_mode='unscoped' and with scope_for_run's reconstruction.
    scope = ConfirmedScope(primary=PRIMARY, secondary=(SECONDARY,), unscoped=True)
    scope_id = record_confirmed_scope(
        db, intent_id="intent_5", generation_run_id="run_5", recognition_id=None, scope=scope,
        use_case_origins={}, confirmation_source="user_broadened", confirmed_by="ds1")

    n_children = db.execute(
        "SELECT count(*) FROM confirmed_scope_use_case WHERE scope_id = %s", (scope_id,)).fetchone()[0]
    assert n_children == 0   # stray primary/secondary ignored on an unscoped scope
    assert scope_for_run(db, "run_5") == ConfirmedScope(primary=None, secondary=(), unscoped=True)


def test_scope_for_run_returns_none_for_unknown_run(db) -> None:
    assert scope_for_run(db, "no_such_run") is None


def test_proposed_vs_accepted_delta_is_derivable(db) -> None:
    """The recognizer proposes primary + secondary, but the human confirms ONLY the primary (drops the
    secondary). The proposed-but-not-accepted delta is computable by joining the attempt's candidates to
    the confirmed child use-cases via recognition_id."""
    rid = record_recognition_attempt(
        db, intent_id="intent_3", input_hash="hash_3", result=_result(), actor="ds1")
    accepted = ConfirmedScope(primary=PRIMARY, secondary=(), unscoped=False)   # secondary dropped
    record_confirmed_scope(
        db, intent_id="intent_3", generation_run_id="run_3", recognition_id=rid, scope=accepted,
        use_case_origins={}, confirmation_source="user_confirmed", confirmed_by="ds1")

    proposed_not_accepted = db.execute(
        """
        SELECT cand->>'use_case_id'
          FROM confirmed_generation_scope s
          JOIN intent_recognition_attempt a ON a.recognition_id = s.recognition_id
          CROSS JOIN LATERAL jsonb_array_elements(a.candidates) AS cand
         WHERE s.generation_run_id = %s
           AND NOT EXISTS (
                 SELECT 1 FROM confirmed_scope_use_case c
                  WHERE c.scope_id = s.scope_id AND c.use_case_id = cand->>'use_case_id')
         ORDER BY 1
        """,
        ("run_3",)).fetchall()
    assert [r[0] for r in proposed_not_accepted] == [SECONDARY]  # proposed but NOT accepted


def test_duplicate_generation_run_id_scope_is_rejected(db) -> None:
    scope = ConfirmedScope(primary=PRIMARY, unscoped=False)
    record_confirmed_scope(
        db, intent_id="intent_4", generation_run_id="run_4", recognition_id=None, scope=scope,
        use_case_origins={}, confirmation_source="user_confirmed", confirmed_by="ds1")

    # A second scope for the SAME generation_run_id violates the UNIQUE canonical linkage.
    with pytest.raises(psycopg.errors.UniqueViolation):
        record_confirmed_scope(
            db, intent_id="intent_4", generation_run_id="run_4", recognition_id=None, scope=scope,
            use_case_origins={}, confirmation_source="user_confirmed", confirmed_by="ds1")


def _dim_result() -> RecognitionResult:
    """A CLASSIFIED result whose recognizer PROPOSED two modelling contexts (``ifrs9``/``frtb``) and a
    soft ``target_entity`` (``account``) — the proposed half the confirmation delta reconciles against."""
    return RecognitionResult(
        status=RecognitionStatus.CLASSIFIED,
        candidates=(UseCaseCandidate(use_case_id=PRIMARY, relationship="primary", confidence="high",
                                     evidence_spans=("balance dropped",), rationale="churn signal"),),
        ambiguity_note=None,
        taxonomy_version="tax_v9", recognizer_model_id="model_v9", prompt_version="prompt_v9",
        applicability_mapping_version="map_v9", recipe_registry_version="reg_v9",
        modelling_contexts=("ifrs9", "frtb"),
        target_entity="account",
    )


def test_confirmed_dimensions_persist_provenance_round_trip_and_delta(db) -> None:
    """The recognizer proposes contexts ifrs9/frtb + entity account; the human accepts ifrs9, rejects
    frtb, adds lcr, and replaces the entity account->customer. The dimension child rows carry the right
    provenance, scope_for_run rebuilds the confirmed dimensions, and confirmation_delta reconstructs the
    proposed-vs-confirmed delta."""
    rid = record_recognition_attempt(
        db, intent_id="intent_dimscope", input_hash="hash_dimscope", result=_dim_result(), actor="ds1")

    scope = ConfirmedScope(
        primary=PRIMARY, secondary=(), expansion=ScopeExpansion.EXACT, unscoped=False,
        modelling_contexts=("ifrs9", "lcr"),   # ifrs9 accepted, frtb rejected (dropped), lcr added
        target_entity="customer")              # replaces the proposed account
    scope_id = record_confirmed_scope(
        db, intent_id="intent_dimscope", generation_run_id="run_dimscope", recognition_id=rid,
        scope=scope, use_case_origins={},
        dimension_sources={"lcr": "user_added", "customer": "user_replacement"},
        replaces={"customer": "account"},
        confirmation_source="user_confirmed", confirmed_by="ds1")

    # The dimension child rows carry dimension / value / source / replaces_value.
    rows = db.execute(
        "SELECT dimension, value, source, replaces_value, display_order "
        "FROM confirmed_scope_dimension WHERE scope_id = %s ORDER BY dimension, display_order",
        (scope_id,)).fetchall()
    assert rows == [
        ("modelling_context", "ifrs9", "accepted_llm_proposal", None, 0),  # accepted, default source
        ("modelling_context", "lcr", "user_added", None, 1),               # user added
        ("target_entity", "customer", "user_replacement", "account", 0),   # replaces account
    ]

    # scope_for_run rebuilds the confirmed dimensions verbatim (ordered contexts + single entity).
    reconstructed = scope_for_run(db, "run_dimscope")
    assert reconstructed == scope
    assert reconstructed.modelling_contexts == ("ifrs9", "lcr")
    assert reconstructed.target_entity == "customer"

    # The proposed-vs-confirmed delta reconstructs from the attempt proposals + confirmed child rows.
    delta = confirmation_delta(db, "run_dimscope")
    assert delta["accepted"] == ["ifrs9"]                 # proposed AND confirmed
    assert delta["rejected"] == ["frtb"]                  # proposed, dropped (account is REPLACED, not here)
    assert set(delta["added"]) == {"lcr", "customer"}     # confirmed, not proposed (lcr + the entity change)
    assert delta["replaced"] == [{"from": "account", "to": "customer"}]


def test_unscoped_scope_writes_no_dimension_rows(db) -> None:
    # An unscoped scope confirms no dimensions -> ZERO confirmed_scope_dimension rows, even if a stray
    # modelling_context/target_entity rode in on the value object (mirrors the use-case-children guard).
    scope = ConfirmedScope(
        primary=None, unscoped=True, modelling_contexts=("ifrs9",), target_entity="customer")
    scope_id = record_confirmed_scope(
        db, intent_id="intent_undim", generation_run_id="run_undim", recognition_id=None, scope=scope,
        use_case_origins={}, confirmation_source="user_broadened", confirmed_by="ds1")

    n_dims = db.execute(
        "SELECT count(*) FROM confirmed_scope_dimension WHERE scope_id = %s", (scope_id,)).fetchone()[0]
    assert n_dims == 0
    assert scope_for_run(db, "run_undim") == ConfirmedScope(primary=None, secondary=(), unscoped=True)


def test_scope_with_no_dimensions_round_trips_as_empty(db) -> None:
    # Backward-compat: a scope confirming no dimensions round-trips as ()/None (Phase-1B-identical).
    scope = ConfirmedScope(primary=PRIMARY, secondary=(SECONDARY,), unscoped=False)
    record_confirmed_scope(
        db, intent_id="intent_nodimscope", generation_run_id="run_nodimscope", recognition_id=None,
        scope=scope, use_case_origins={}, confirmation_source="user_confirmed", confirmed_by="ds1")

    reconstructed = scope_for_run(db, "run_nodimscope")
    assert reconstructed == scope
    assert reconstructed.modelling_contexts == ()
    assert reconstructed.target_entity is None


def test_confirmation_delta_unknown_run_is_empty(db) -> None:
    assert confirmation_delta(db, "no_such_run") == {
        "accepted": [], "rejected": [], "added": [], "replaced": []}
