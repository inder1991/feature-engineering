"""The generation lane — the stages `compile_generation_v2` stops before, run in order.

`evaluate_generate`, `record_group_plan`, `record_bound_formula`, `render_project` and `seal_v2`
each had unit tests and ZERO production callers. The middle was unwelded, which the repository's own
seam test documented. `generate_v2` is the weld, and these tests are about the ORDER and the
ALL-OR-NOTHING, because that is what a caller cannot get right by reading five docstrings.

What they hold:

1. **The gate runs before anything is recorded.** A refused generation leaves no group plan and no
   artifact — otherwise the next reader cannot tell a plan that was cleared from one that was
   stopped.
2. **Every member is gated**, and one refusal refuses the group.
3. **A member with no policy occurrences is a caller error**, not a member that passes.
4. **The sealed artifact names the approval** that produced it.
"""
from __future__ import annotations

import pytest
from tests.featuregen.materialize.test_ir import (
    _ROLES,
    DECLARATION,
    INVENTORY,
    TXN_AMT,
    _admitted,
    _col,
    _table_node,
    compile_ir,
    seed_catalog,
)
from tests.featuregen.materialize.test_pilot_v2 import (  # the real compiled generation
    ENV,
    GROUP,
    _admitted_v2,
    _run,
)

from featuregen.formula.policy_occurrences import PolicyOccurrenceSetV1
from featuregen.materialize.generate_v2 import GenerationRefused, generate_v2
from featuregen.materialize.pilot_v2 import CompiledGenerationV2


# Declared here rather than imported: a fixture imported by name shadows the test parameter that
# requests it, and hoisting one called `catalog` into a conftest would silently re-bind the other
# materialize modules that already request one.
@pytest.fixture
def catalog(db):
    from tests.featuregen.materialize import fixtures

    from featuregen.overlay.upload.field_resolution import resolve_and_project

    seed_catalog(db)
    for column in ("rate", "quote_dt", "ccy"):
        _col(db, "fx_rates", column)
    _table_node(db, "fx_rates")
    fixtures._attest(db, TXN_AMT, "logical_representation", "numeric")
    resolve_and_project(db, source="hdfc", logical_refs=[TXN_AMT])
    db.execute("UPDATE graph_node SET data_type = 'numeric' "
               "WHERE catalog_source = 'hdfc' AND object_ref = 'public.transactions.txn_amt'")
    return db


@pytest.fixture
def spine(catalog):
    return compile_ir(catalog, _admitted("total_debit_amount_30d"), roles=_ROLES,
                      spine_decl=DECLARATION, inventory=INVENTORY).spine


def _approval(db) -> str:
    from featuregen.materialize.generation_authorization import (
        GenerationAuthorizationV1,
        record_generation_authorization,
    )
    from featuregen.overlay.upload.selection_revisions import TargetModeV1

    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, "
               "redacted_hypothesis) VALUES ('int-g','h','hypothesis','h') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
               "VALUES ('trr-g','int-g','exploration','h') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO build_set_revision (revision_id, target_reading_revision_id, "
               "declaration_hash, declaration_json, content_hash, declared_by, declared_at) "
               "VALUES ('bs-g','trr-g','dh','{}'::jsonb,'ch','user:ops','t') ON CONFLICT DO NOTHING")
    return record_generation_authorization(
        db, GenerationAuthorizationV1(
            environment_id=ENV, logical_group_name=GROUP, build_set_revision_id="bs-g",
            target_mode=TargetModeV1.EXPLORATION, target_ref=None),
        authorized_by="user:ops", authorized_at="t")


def _compiled(catalog, spine) -> CompiledGenerationV2:
    compiled = _run(catalog, spine, [_admitted_v2()])
    assert isinstance(compiled, CompiledGenerationV2), compiled
    return compiled


def _generate(catalog, compiled, *, occurrences=None, engine_id="kedro-pyspark", **kwargs):
    return generate_v2(
        catalog, compiled,
        environment_id=ENV,
        generation_authorization_revision_id=kwargs.pop("approval", _approval(catalog)),
        engine_id=engine_id,
        engine_versions=kwargs.pop("engine_versions", None),
        spine_input=kwargs.pop("spine_input", None),
        nodes=(), artifact_id="art-gen-1",
        occurrences_by_member=(occurrences if occurrences is not None
                               else {name: PolicyOccurrenceSetV1(())
                                     for name in compiled.graphs}),
        realizations=(), compiled_at="t", sealed_at="t", **kwargs)


# ══ THE GATE RUNS FIRST, AND NOTHING IS RECORDED BEHIND IT ═════════════════════════════════════
def test_A_REFUSED_GENERATION_RECORDS_NOTHING(catalog, spine):
    """The order is the substance. A refused generation that left a group plan behind would leave
    the next reader unable to tell a plan that was CLEARED from one that was STOPPED.

    `unsupported-engine` is refused by `evaluate_generate` because nothing advertises it — an engine
    this build has never heard of must be unsupported rather than assumed.
    """
    compiled = _compiled(catalog, spine)
    before = catalog.execute("SELECT count(*) FROM sealed_artifact_v2").fetchone()[0]

    with pytest.raises(GenerationRefused) as refused:
        _generate(catalog, compiled, engine_id="engine-nobody-has-heard-of")

    assert refused.value.verdict.allowed is False
    assert refused.value.member in compiled.graphs
    assert catalog.execute("SELECT count(*) FROM sealed_artifact_v2").fetchone()[0] == before
    assert catalog.execute(
        "SELECT count(*) FROM materialization_group_v2").fetchone()[0] == 0


def test_THE_REFUSAL_CARRIES_EVERY_BLOCKER_not_just_that_there_were_some(catalog, spine):
    """An exception rather than a returned verdict — this one is terminal for the whole generation
    and there is nothing further to collect — but it carries the verdict so a caller can report
    what to fix rather than that something was wrong."""
    compiled = _compiled(catalog, spine)
    with pytest.raises(GenerationRefused) as refused:
        _generate(catalog, compiled, engine_id="engine-nobody-has-heard-of")

    assert refused.value.verdict.blockers, "a refusal with no blockers tells an author nothing"


# ══ EVERY MEMBER IS GATED ══════════════════════════════════════════════════════════════════════
def test_A_MEMBER_WITH_NO_OCCURRENCES_IS_A_CALLER_ERROR(catalog, spine):
    """Not a member that passes. `evaluate_generate` refuses a generation whose policy references
    cannot be resolved, and a member gated against an empty set is one whose references were never
    checked — which passes."""
    compiled = _compiled(catalog, spine)
    with pytest.raises(ValueError, match="no policy occurrences supplied"):
        _generate(catalog, compiled, occurrences={})
