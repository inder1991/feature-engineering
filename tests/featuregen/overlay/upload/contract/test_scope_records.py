"""Phase-1B Task 2 — scope-record persistence round-trips (real `db` connection).

Covers the recognition -> run -> scope lineage: an idempotent attempt (quintet stamped, proposals
retained), a confirmed scope with normalized child rows (relationship/origin/display_order),
`scope_for_run` reconstruction, the queryable proposed-vs-accepted delta, and the unique canonical
run->scope linkage.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.intake.llm import compute_input_hash
from featuregen.overlay.upload.contract.scope_records import (
    confirmation_delta,
    dimension_provenance,
    find_recognition_attempt,
    generation_input_for_run,
    load_recognition_attempt,
    load_recognition_input,
    recognition_input_material,
    record_confirmed_scope,
    record_generation_input,
    record_recognition_attempt,
    scope_for_run,
    use_case_provenance,
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


def test_sealed_recognition_and_generation_input_round_trip_and_are_write_once(db) -> None:
    intent_id = "intent_sealed_input"
    run_id = "run_sealed_input"
    db.execute(
        "INSERT INTO contract_intent "
        "(intent_id, hypothesis, redacted_hypothesis, intake_mode, actor) "
        "VALUES (%s, %s, %s, 'hypothesis', %s::jsonb)",
        (intent_id, "customers churn", "customers churn", '"ds1"'),
    )
    material = recognition_input_material(
        redacted_hypothesis="customers churn",
        redacted_prediction_goal="predict churn",
        redaction_policy_version="default-redactor@1",
    )
    input_hash = compute_input_hash(material)
    recognition_id = record_recognition_attempt(
        db,
        intent_id=intent_id,
        input_hash=input_hash,
        result=_result(),
        actor="ds1",
        input_json=material,
        redaction_policy_version="default-redactor@1",
    )
    recognition = load_recognition_input(
        db, recognition_id=recognition_id, intent_id=intent_id)
    assert recognition.redacted_prediction_goal == "predict churn"
    assert recognition.input_content_hash == input_hash

    db.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
        "VALUES (%s, %s, '{}'::jsonb)",
        (run_id, intent_id),
    )
    scope_id = record_confirmed_scope(
        db,
        intent_id=intent_id,
        generation_run_id=run_id,
        recognition_id=recognition_id,
        scope=ConfirmedScope(primary=PRIMARY),
        use_case_origins={},
        confirmation_source="user_confirmed",
        confirmed_by="ds1",
    )
    recorded = record_generation_input(
        db,
        generation_run_id=run_id,
        intent_id=intent_id,
        recognition=recognition,
        confirmed_scope_id=scope_id,
        redacted_definition="",
        redacted_feedback="keep explainable",
        target_ref="public.labels.churned",
        actor="ds1",
    )
    loaded = generation_input_for_run(db, run_id)
    assert loaded == recorded
    assert loaded is not None and loaded.target_ref == "public.labels.churned"

    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), db.transaction():
        db.execute(
            "UPDATE contract_generation_input SET target_ref = NULL "
            "WHERE generation_run_id = %s",
            (run_id,),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), db.transaction():
        db.execute(
            "UPDATE intent_recognition_attempt SET input_json = '{}'::jsonb "
            "WHERE recognition_id = %s",
            (recognition_id,),
        )


def test_generation_input_database_rejects_cross_run_lineage(db) -> None:
    intent_id = "intent_bad_generation_lineage"
    run_id = "run_bad_generation_lineage"
    db.execute(
        "INSERT INTO contract_intent "
        "(intent_id, hypothesis, redacted_hypothesis, intake_mode, actor) "
        "VALUES (%s, 'h', 'h', 'hypothesis', %s::jsonb)",
        (intent_id, '"ds1"'),
    )
    material = recognition_input_material(
        redacted_hypothesis="h",
        redacted_prediction_goal="g",
        redaction_policy_version="default-redactor@1",
    )
    recognition_id = record_recognition_attempt(
        db,
        intent_id=intent_id,
        input_hash=compute_input_hash(material),
        result=_result(),
        actor="ds1",
        input_json=material,
        redaction_policy_version="default-redactor@1",
    )
    db.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
        "VALUES (%s, %s, '{}'::jsonb)",
        (run_id, intent_id),
    )
    scope_id = record_confirmed_scope(
        db,
        intent_id=intent_id,
        generation_run_id=run_id,
        recognition_id=recognition_id,
        scope=ConfirmedScope(primary=PRIMARY),
        use_case_origins={},
        confirmation_source="user_confirmed",
        confirmed_by="ds1",
    )
    with pytest.raises(psycopg.errors.RaiseException, match="lineage is inconsistent"), db.transaction():
        db.execute(
            "INSERT INTO contract_generation_input "
            "(generation_run_id, intent_id, recognition_id, confirmed_scope_id, "
            "redacted_hypothesis, redacted_definition, redacted_prediction_goal, "
            "redacted_feedback, recognition_input_content_hash, generation_input_content_hash, "
            "created_by) VALUES (%s, %s, %s, %s, 'h', '', 'g', '', 'wrong', 'hash', '{}'::jsonb)",
            (run_id, intent_id, recognition_id, scope_id),
        )


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
        (PRIMARY, "primary", "accepted_llm_proposal", 0),
        (SECONDARY, "secondary", "user_added", 1),   # secondary origin overridden, order 1
    ]

    # scope_for_run reconstructs the EXACT ConfirmedScope by run id only (canonical linkage).
    reconstructed = scope_for_run(db, "run_1")
    assert reconstructed == scope


def test_use_case_provenance_derives_role_overrides_and_replacement(db) -> None:
    rid = record_recognition_attempt(
        db, intent_id="intent_role_override", input_hash="hash_role_override",
        result=_result(), actor="ds1")
    promoted = ConfirmedScope(primary=SECONDARY, secondary=(PRIMARY,))

    origins, relationships, replacements = use_case_provenance(db, rid, promoted)

    assert origins == {
        SECONDARY: "user_overridden",
        PRIMARY: "user_overridden",
    }
    assert relationships == {
        SECONDARY: "secondary",
        PRIMARY: "primary",
    }
    assert replacements == {SECONDARY: PRIMARY}

    scope_id = record_confirmed_scope(
        db,
        intent_id="intent_role_override",
        generation_run_id="run_role_override",
        recognition_id=rid,
        scope=promoted,
        use_case_origins=origins,
        use_case_proposed_relationships=relationships,
        use_case_replacements=replacements,
        confirmation_source="user_confirmed",
        confirmed_by="ds1",
    )
    rows = db.execute(
        "SELECT use_case_id, relationship, origin, proposed_relationship, replaces_use_case_id "
        "FROM confirmed_scope_use_case WHERE scope_id = %s ORDER BY display_order",
        (scope_id,),
    ).fetchall()
    assert rows == [
        (SECONDARY, "primary", "user_overridden", "secondary", PRIMARY),
        (PRIMARY, "secondary", "user_overridden", "primary", None),
    ]


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


def test_unscoped_scope_persists_and_rebuilds_dimension_rows(db) -> None:
    # Fix 1: the confirmed dimensions are DATA orthogonal to use-case scoping — an unscoped broaden
    # ("show all buildable recipes") does NOT forget the confirmed IFRS9 context. So an UNSCOPED scope
    # PERSISTS its dimension rows (unlike the use-case children) and scope_for_run rebuilds them, while
    # still writing ZERO use-case child rows.
    scope = ConfirmedScope(
        primary=None, unscoped=True, modelling_contexts=("ifrs9",), target_entity="customer")
    scope_id = record_confirmed_scope(
        db, intent_id="intent_undim", generation_run_id="run_undim", recognition_id=None, scope=scope,
        use_case_origins={}, confirmation_source="user_broadened", confirmed_by="ds1")

    # ZERO use-case child rows (a broaden confirms no use-cases) …
    n_children = db.execute(
        "SELECT count(*) FROM confirmed_scope_use_case WHERE scope_id = %s", (scope_id,)).fetchone()[0]
    assert n_children == 0
    # … but the confirmed DIMENSION rows DO persist for the unscoped scope.
    dims = db.execute(
        "SELECT dimension, value FROM confirmed_scope_dimension WHERE scope_id = %s "
        "ORDER BY dimension, display_order", (scope_id,)).fetchall()
    assert dims == [("modelling_context", "ifrs9"), ("target_entity", "customer")]

    # scope_for_run rebuilds an unscoped scope that STILL carries its confirmed dimensions (round-trip).
    reconstructed = scope_for_run(db, "run_undim")
    assert reconstructed == scope
    assert reconstructed.unscoped is True
    assert reconstructed.modelling_contexts == ("ifrs9",)
    assert reconstructed.target_entity == "customer"


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


# ── Fix 2: server-side dimension provenance is derived from the IMMUTABLE attempt, never the client ────
def test_dimension_provenance_derives_mixed_sources_from_the_attempt(db) -> None:
    # The recognizer PROPOSED contexts ifrs9/frtb + entity account. The human keeps ifrs9 (accepted),
    # adds lcr (never proposed), and corrects the entity account -> customer. Provenance is reconstructed
    # from the stored attempt, not from any client claim.
    rid = record_recognition_attempt(
        db, intent_id="intent_prov", input_hash="hash_prov", result=_dim_result(), actor="ds1")
    scope = ConfirmedScope(
        primary=PRIMARY, secondary=(), unscoped=False,
        modelling_contexts=("ifrs9", "lcr"), target_entity="customer")

    sources, replaces = dimension_provenance(db, rid, scope)
    assert sources["ifrs9"] == "accepted_llm_proposal"    # proposed AND confirmed
    assert sources["lcr"] == "user_added"                 # confirmed, never proposed
    assert sources["customer"] == "user_replacement"      # a DIFFERENT entity (account) was proposed
    assert replaces == {"customer": "account"}            # supersedes the proposed account


def test_dimension_provenance_accepted_entity_and_contexts(db) -> None:
    # The human keeps EXACTLY what the recognizer proposed (ifrs9/frtb + account) -> all accepted.
    rid = record_recognition_attempt(
        db, intent_id="intent_prov_acc", input_hash="hash_prov_acc", result=_dim_result(), actor="ds1")
    scope = ConfirmedScope(
        primary=PRIMARY, unscoped=False, modelling_contexts=("ifrs9", "frtb"), target_entity="account")
    sources, replaces = dimension_provenance(db, rid, scope)
    assert sources == {"ifrs9": "accepted_llm_proposal", "frtb": "accepted_llm_proposal",
                       "account": "accepted_llm_proposal"}
    assert replaces == {}


def test_dimension_provenance_entity_added_when_none_proposed(db) -> None:
    # The recognizer proposed NO target_entity -> a human-set entity is user_added, not a replacement.
    result = RecognitionResult(
        status=RecognitionStatus.CLASSIFIED,
        candidates=(UseCaseCandidate(use_case_id=PRIMARY, relationship="primary", confidence="high",
                                     evidence_spans=("x",), rationale="y"),),
        ambiguity_note=None, taxonomy_version="t", recognizer_model_id="m", prompt_version="p",
        applicability_mapping_version="a", recipe_registry_version="r",
        modelling_contexts=("ifrs9",), target_entity=None)
    rid = record_recognition_attempt(
        db, intent_id="intent_prov_add", input_hash="hash_prov_add", result=result, actor="ds1")
    scope = ConfirmedScope(primary=PRIMARY, unscoped=False, target_entity="customer")

    sources, replaces = dimension_provenance(db, rid, scope)
    assert sources["customer"] == "user_added"
    assert replaces == {}


def test_dimension_provenance_no_recognition_is_all_user_added(db) -> None:
    # No linked recognition (recognition_id=None) -> no proposals -> every confirmed value is user_added.
    scope = ConfirmedScope(
        primary=PRIMARY, unscoped=False, modelling_contexts=("ifrs9",), target_entity="customer")
    sources, replaces = dimension_provenance(db, None, scope)
    assert sources == {"ifrs9": "user_added", "customer": "user_added"}
    assert replaces == {}


def test_provenance_wired_into_record_yields_truthful_delta(db) -> None:
    # Fix 2 end-to-end (the route's wiring): derive provenance from the attempt, feed it into
    # record_confirmed_scope, and confirmation_delta reads a TRUTHFUL accepted/rejected/added/replaced
    # split — with NO hand-supplied provenance.
    rid = record_recognition_attempt(
        db, intent_id="intent_wire", input_hash="hash_wire", result=_dim_result(), actor="ds1")
    scope = ConfirmedScope(
        primary=PRIMARY, secondary=(), unscoped=False,
        modelling_contexts=("ifrs9", "lcr"), target_entity="customer")
    sources, replaces = dimension_provenance(db, rid, scope)
    scope_id = record_confirmed_scope(
        db, intent_id="intent_wire", generation_run_id="run_wire", recognition_id=rid, scope=scope,
        use_case_origins={}, dimension_sources=sources, replaces=replaces,
        confirmation_source="user_confirmed", confirmed_by="ds1")

    rows = db.execute(
        "SELECT dimension, value, source, replaces_value FROM confirmed_scope_dimension "
        "WHERE scope_id = %s ORDER BY dimension, display_order", (scope_id,)).fetchall()
    assert rows == [
        ("modelling_context", "ifrs9", "accepted_llm_proposal", None),
        ("modelling_context", "lcr", "user_added", None),
        ("target_entity", "customer", "user_replacement", "account")]

    delta = confirmation_delta(db, "run_wire")
    assert delta["accepted"] == ["ifrs9"]                 # proposed AND confirmed
    assert delta["rejected"] == ["frtb"]                  # proposed, dropped (account is REPLACED, not here)
    assert set(delta["added"]) == {"lcr", "customer"}     # confirmed, not proposed
    assert delta["replaced"] == [{"from": "account", "to": "customer"}]


# ── Task 0 (2026-08-15): request identity, and reading the answer back ──────────────────────────


def test_a_stored_attempt_round_trips_as_the_result_it_was(db) -> None:
    """The response is built from the ROW, so the row must rebuild the whole result — status,
    both proposals with their spans, the quintet, the dimensions and the warnings. Anything this
    read drops would silently disappear from every served re-run."""
    result = RecognitionResult(
        status=RecognitionStatus.CLASSIFIED,
        candidates=_result().candidates,
        ambiguity_note="a note",
        taxonomy_version="tax_v9", recognizer_model_id="model_v9", prompt_version="prompt_v9",
        applicability_mapping_version="map_v9", recipe_registry_version="reg_v9",
        modelling_contexts=("ifrs9",), target_entity="customer",
        warnings=("UNKNOWN_TARGET_ENTITY",))
    rid = record_recognition_attempt(
        db, intent_id="intent_rt", input_hash="req_rt", result=result, actor="ds1",
        input_content_hash="content_rt", recognition_request_hash="req_rt",
        llm_call_ref="llm_rt")
    stored = load_recognition_attempt(db, recognition_id=rid)
    assert stored is not None
    assert stored.recognition_id == rid
    assert stored.result == result
    assert stored.llm_call_ref == "llm_rt"
    assert stored.recognition_request_hash == "req_rt"


def test_an_unknown_recognition_id_loads_as_absent(db) -> None:
    assert load_recognition_attempt(db, recognition_id="rcg_nope") is None


def test_a_legacy_row_is_never_found_by_request_identity(db) -> None:
    """FAIL CLOSED on the legacy NULL. A pre-1070 row was keyed by user input alone, so nobody knows
    which prompt, schema, validator or model produced it — serving it as the answer to a request
    whose identity was never recorded is exactly the confusion Task 0 exists to end. The cost is one
    provider call on the first re-run of a legacy objective, and that is the honest price."""
    record_recognition_attempt(
        db, intent_id="intent_legacy", input_hash="content_only", result=_result(), actor="ds1")
    assert find_recognition_attempt(
        db, intent_id="intent_legacy", recognition_request_hash="content_only") is None


def test_the_stored_answer_is_found_by_request_identity(db) -> None:
    rid = record_recognition_attempt(
        db, intent_id="intent_f", input_hash="req_f", result=_result(), actor="ds1",
        input_content_hash="content_f", recognition_request_hash="req_f")
    found = find_recognition_attempt(
        db, intent_id="intent_f", recognition_request_hash="req_f")
    assert found is not None and found.recognition_id == rid
    # Another intent's identical request is a different question.
    assert find_recognition_attempt(
        db, intent_id="intent_other", recognition_request_hash="req_f") is None


def test_a_request_identity_row_must_key_on_its_own_hash(db) -> None:
    """The 1070 CHECK, refused in code too, so a caller learns it from a name and not a constraint."""
    with pytest.raises(ValueError, match="key on its own recognition_request_hash"):
        record_recognition_attempt(
            db, intent_id="intent_bad", input_hash="content_bad", result=_result(), actor="ds1",
            input_content_hash="content_bad", recognition_request_hash="req_bad")


def test_a_request_identity_row_must_seal_its_input_separately(db) -> None:
    """Defaulting the sealed content hash from the KEY is exactly the conflation Task 0 undoes."""
    with pytest.raises(ValueError, match="seal input_content_hash separately"):
        record_recognition_attempt(
            db, intent_id="intent_bad2", input_hash="req_bad2", result=_result(), actor="ds1",
            recognition_request_hash="req_bad2")


def test_the_sealed_input_is_verified_against_the_content_hash_not_the_key(db) -> None:
    """The sealed material must re-derive `input_content_hash` — the request hash never could, and a
    guard that compared against the KEY would have to be relaxed into uselessness to let it pass."""
    material = recognition_input_material(
        redacted_hypothesis="customers churn", redacted_prediction_goal="predict churn",
        redaction_policy_version="r1")
    content_hash = compute_input_hash(material)
    rid = record_recognition_attempt(
        db, intent_id="intent_seal", input_hash="req_seal", result=_result(), actor="ds1",
        input_json=material, redaction_policy_version="r1",
        input_content_hash=content_hash, recognition_request_hash="req_seal")
    sealed = load_recognition_input(db, recognition_id=rid, intent_id="intent_seal")
    assert sealed.input_content_hash == content_hash
    with pytest.raises(ValueError, match="does not match input_json"):
        record_recognition_attempt(
            db, intent_id="intent_seal2", input_hash="req_seal2", result=_result(), actor="ds1",
            input_json=material, redaction_policy_version="r1",
            input_content_hash="not_the_content_hash", recognition_request_hash="req_seal2")
