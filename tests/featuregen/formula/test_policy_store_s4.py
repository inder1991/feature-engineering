"""S4 — the policy resolver (1075), against all four acceptance clauses.

*"An unresolvable reference refuses by name; an operand whose C1 facts are not ``monetary``/
``per_row`` needs no currency occurrence; conflicting source and LLM evidence resolves to source with
the conflict retained; no V2 path writes through the mutable upsert."*

Every clause is asserted through the store against a real database, because three of the four are
claims about persistence: a conflict that is retained in memory and dropped on write is not
retained, and a path that avoids the mutable upsert in prose can still reach it through an import.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import psycopg
import pytest

from featuregen.data_agent import eligibility_store
from featuregen.formula import policy_store
from featuregen.formula.measure_facts import (
    MeasureFact,
    MeasureFacts,
    MeasureReadDisposition,
)
from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.parse_v3 import parse_proposal_v3
from featuregen.formula.policy_admissibility import (
    NO_CANDIDATE,
    NO_DETERMINISTIC_WINNER,
    SOURCE_OVERRODE_LLM,
    TWO_GOVERNED_DECLARATIONS,
    AdmissibilityOutcomeV1,
)
from featuregen.formula.policy_occurrences import (
    PolicyOccurrenceSetV1,
    PolicyOccurrenceV1,
    derive_policy_occurrences,
)
from featuregen.formula.policy_producer import realization_from_payload
from featuregen.formula.policy_realization import (
    ConflictFindingV1,
    PolicyRealizationRevisionV1,
    RealizationProvenanceV1,
    family_key_for,
)
from featuregen.formula.policy_store import (
    OccurrenceProvenanceMissing,
    PolicyPointerConflict,
    PolicyRealizationRefused,
    PolicyReferenceUnresolvable,
    PolicyStoreCorrupt,
    load_realization_revision,
    measure_reads_for,
    operand_facts_from_measure,
    publish_policy_realization,
    record_occurrence_set,
    record_realization_revision,
    resolve_policy_occurrence,
    set_current_realization,
    unmet_policy_kinds,
    unresolved_references,
)
from featuregen.formula.schema_v3 import FORMULA_SCHEMA_VERSION_V3
from featuregen.materialize.inventory import ClusterInventoryV1, EngineVersions, TableLayout
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.inventory_revisions import (
    BoundInputSetRevisionV2,
    BoundInputV2,
    GenerationInventoryObservationV1,
)
from featuregen.overlay.upload.inventory_store import (
    record_bound_input_set,
    record_inventory_observation,
)

_GOLD_V2 = Path(__file__).parent / "gold_v2"
MIGRATION = Path("src/featuregen/db/migrations/1075_policy_occurrences_and_realizations.sql")

ENV = "hdfc-local"
DATASET = "public.transactions"
DIR_REF = "direction_sign:foundation-signed-by-indicator"
FX_REF = "currency_conversion:foundation-base-currency"
TXN = "hdfc::public.transactions.txn_amt"
ACCT = "hdfc::public.transactions.acct_id"


# ══ fixtures — the S3 binding an occurrence set must hang off ════════════════════════════════════
def _layout() -> TableLayout:
    return TableLayout(
        schema="public", table="transactions", partition_columns=(("load_dt", "string"),),
        partition_mapping=None, columns=(("txn_amt", "decimal(18,2)"), ("acct_id", "string")),
        location="hdfs://nn/warehouse/public.db/transactions", rewritten_in_place=False)


def _seed_binding(db, revision_id: str = "bis-s4") -> str:
    """An inventory observation and a bound input set — S4's occurrences reference the second."""
    observation = GenerationInventoryObservationV1(
        observation_id="obs-s4",
        inventory=ClusterInventoryV1(
            environment_id=ENV, tables={"public.transactions": _layout()},
            logical_schema_map={"hdfc::public.transactions": "public"},
            engine_versions=EngineVersions(
                hive="3.1.2", spark="3.3.0", metastore="3.1.2", python="3.11.14",
                java="11.0.20", pyspark="3.3.0", kedro="0.19.3", kedro_datasets="2.1.0"),
            captured_at="2026-08-17T00:00:00Z"),
        used_logical_schema_refs=("hdfc::public.transactions",),
        read_set=(TXN, ACCT))
    record_inventory_observation(db, observation, captured_at="2026-08-17T00:00:00Z")
    record_bound_input_set(
        db,
        BoundInputSetRevisionV2(
            revision_id=revision_id, environment_id=ENV,
            inputs=(BoundInputV2(TXN, DATASET, "txn_amt"),
                    BoundInputV2(ACCT, DATASET, "acct_id"))),
        inventory_observation_id="obs-s4")
    return revision_id


def _occurrence(
    *, role: str = "direction", ref: str = DIR_REF, kind: str = "direction_sign",
    field: str = "direction_policy_ref", expr_path: str = "body.expr",
    column: str = "txn_amt", dataset: str = DATASET, environment: str = ENV,
) -> PolicyOccurrenceV1:
    return PolicyOccurrenceV1(
        expr_path=expr_path, policy_ref_field=field, policy_kind=kind, policy_ref=ref,
        semantic_role=role, bound_dataset=dataset, bound_column=column,
        environment_id=environment)


def _revision(
    occurrence: PolicyOccurrenceV1, *, revision_id: str, executable: str,
    provenance: RealizationProvenanceV1 = RealizationProvenanceV1.SOURCE_DERIVED,
    conflicts: tuple[ConflictFindingV1, ...] = (),
) -> PolicyRealizationRevisionV1:
    return PolicyRealizationRevisionV1(
        revision_id=revision_id, family_key=family_key_for(occurrence),
        executable_content_hash=executable, cas_pointer=f"cas://{executable}",
        provenance=provenance, realizes_occurrences=(occurrence.occurrence_hash,),
        conflict_findings=conflicts)


def _measure(
    *, unit: str = "monetary", currency: str = "per_row",
    disposition: MeasureReadDisposition = MeasureReadDisposition.RESOLVED,
    logical_ref: str = TXN,
) -> MeasureFacts:
    """A verified measure read. `absent` carries an empty value — the honest shape of "nobody has
    decided this column's unit", which is the ordinary case for most columns."""
    def fact(field: str, value: str) -> MeasureFact:
        empty = disposition is MeasureReadDisposition.ABSENT
        return MeasureFact(
            field=field, value="" if empty else value, disposition=disposition,
            producer=None if empty else "source-attestation",
            strength=None if empty else "attested",
            decision_event_id=None if empty else f"decision-{field}",
            selected_evidence_ids=() if empty else (f"evidence-{field}",),
            policy_version="field-policy-7", resolver_version=None if empty else "resolver-3")

    return MeasureFacts(logical_ref=logical_ref, unit=fact("unit", unit),
                        currency=fact("currency", currency))


def _reads(*occurrences: PolicyOccurrenceV1, facts: MeasureFacts | None = None):
    return {o.occurrence_hash: facts or _measure() for o in occurrences}


def _expr(*, refs=..., selections=None):
    raw = json.loads((_GOLD_V2 / "01_avg_txn_amt_90d.json").read_text())["proposal"]
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    expr = raw["body"]["expr"]
    expr["authority_refs"] = {"direction_policy_ref": DIR_REF} if refs is ... else refs
    expr["row_selections"] = selections if selections is not None else []
    return parse_proposal_v3(raw).body.expr


# ══ ACCEPTANCE 1 — an unresolvable reference REFUSES BY NAME ═════════════════════════════════════
def test_AN_UNRESOLVABLE_REFERENCE_REFUSES_BY_NAME(db):
    """Nothing is current for the family, and the refusal says so under the platform's own name
    rather than returning None for a caller to ignore."""
    with pytest.raises(PolicyReferenceUnresolvable) as raised:
        resolve_policy_occurrence(db, _occurrence())

    assert raised.value.refusal_code == R.POLICY_REFERENCE_UNRESOLVABLE
    assert R.POLICY_REFERENCE_UNRESOLVABLE in str(raised.value)
    # The message names every part of the family, because "unresolvable" without them cannot be
    # acted on — which policy, in what role, over which dataset, in which environment.
    for part in (DIR_REF, "direction", DATASET, ENV):
        assert part in str(raised.value), part


def test_the_name_is_in_the_platforms_CLOSED_vocabulary():
    """A refusal outside `REASON_FAMILIES` cannot be grouped or explained on any product surface,
    and the pin test in `test_semantic_eligibility` refuses to let one ship."""
    assert R.reason_family(R.POLICY_REFERENCE_UNRESOLVABLE) == "needs_setup"


def test_it_is_DISTINCT_from_the_blanket_unresolved_code_and_beats_it():
    """`STATUS_POLICY_UNRESOLVED` means no resolver serves this kind at all; the S4 code means the
    resolver ran and found nothing. Two different remedies — build the resolver, or publish a
    realization — so reporting the blanket one when the specific one applies hides which."""
    assert R.POLICY_REFERENCE_UNRESOLVABLE != R.STATUS_POLICY_UNRESOLVED
    order = R.REASON_PRECEDENCE
    assert order.index(R.POLICY_REFERENCE_UNRESOLVABLE) < order.index(R.STATUS_POLICY_UNRESOLVED)


def test_a_reference_WITH_a_current_realization_RESOLVES(db):
    _seed_binding(db)
    occurrence = _occurrence()
    record_occurrence_set(db, PolicyOccurrenceSetV1((occurrence,)), set_id="set-1",
                          bound_input_set_revision_id="bis-s4",
                          measure_reads=_reads(occurrence))
    publish_policy_realization(
        db, [_revision(occurrence, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")

    resolved = resolve_policy_occurrence(db, occurrence)
    assert resolved.revision.revision_id == "rev-1"
    assert resolved.revision.executable_content_hash == "sha256:debit-is-D"
    assert resolved.pointer_version == 1
    assert resolved.claims_occurrence is True


def test_a_SECOND_occurrence_in_the_SAME_family_resolves_without_republication(db):
    """Resolution keys on the family, which is what the family key is defined to be current FOR.
    A second expression reading the same policy over the same column resolves, and reports that the
    realization does not name it — the difference is visible rather than hidden."""
    _seed_binding(db)
    first, second = _occurrence(), _occurrence(expr_path="body.expr.left")
    assert first.occurrence_hash != second.occurrence_hash
    publish_policy_realization(
        db, [_revision(first, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")

    resolved = resolve_policy_occurrence(db, second)
    assert resolved.revision.revision_id == "rev-1"
    assert resolved.claims_occurrence is False


def test_a_DIFFERENT_family_does_NOT_resolve_off_another_familys_pointer(db):
    """The other side: publishing for one dataset must not answer for another, or the pointer is
    current for more than one thing."""
    _seed_binding(db)
    published = _occurrence()
    publish_policy_realization(
        db, [_revision(published, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")

    with pytest.raises(PolicyReferenceUnresolvable):
        resolve_policy_occurrence(db, _occurrence(dataset="public.card_transactions"))


def test_unresolved_references_reports_ONLY_the_unanswered_ones(db):
    _seed_binding(db)
    answered = _occurrence()
    unanswered = _occurrence(role="status", ref="eligible_status:foundation-posted-events",
                             kind="eligible_status", field="status_policy_ref")
    publish_policy_realization(
        db, [_revision(answered, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")

    outstanding = unresolved_references(db, PolicyOccurrenceSetV1((answered, unanswered)))
    assert outstanding == (unanswered,)


def test_admissibility_refusals_keep_THEIR_names(db):
    """The publication refusals are not folded into "unresolvable". "Two governed declarations" is
    fixed upstream in the source; "no deterministic winner" is fixed by getting evidence."""
    _seed_binding(db)
    occurrence = _occurrence()

    two_governed = [
        _revision(occurrence, revision_id="rev-a", executable="sha256:a"),
        _revision(occurrence, revision_id="rev-b", executable="sha256:b"),
    ]
    with pytest.raises(PolicyRealizationRefused) as raised:
        publish_policy_realization(db, two_governed, expected_pointer_version=0,
                                   declared_by="ops@bank")
    assert raised.value.refusal_code == TWO_GOVERNED_DECLARATIONS

    disagreeing_proposals = [
        _revision(occurrence, revision_id="rev-c", executable="sha256:c",
                  provenance=RealizationProvenanceV1.LLM_PROPOSED),
        _revision(occurrence, revision_id="rev-d", executable="sha256:d",
                  provenance=RealizationProvenanceV1.LLM_PROPOSED),
    ]
    with pytest.raises(PolicyRealizationRefused) as raised:
        publish_policy_realization(db, disagreeing_proposals, expected_pointer_version=0,
                                   declared_by="ops@bank")
    assert raised.value.refusal_code == NO_DETERMINISTIC_WINNER


def test_publishing_NOTHING_uses_C_C9s_OWN_name_for_it(db):
    """`NO_CANDIDATE` already means "nothing was offered". A second vocabulary for the same fact
    would put two names in front of the same operator."""
    with pytest.raises(PolicyRealizationRefused) as raised:
        publish_policy_realization(db, [], expected_pointer_version=0, declared_by="ops@bank")
    assert raised.value.refusal_code == NO_CANDIDATE


def test_a_REFUSED_publication_writes_NOTHING(db):
    """A refusal produced no artifact. Leaving revisions behind would put candidates the decision
    rejected one `set_current_realization` call away from being current."""
    _seed_binding(db)
    occurrence = _occurrence()
    with pytest.raises(PolicyRealizationRefused):
        publish_policy_realization(
            db,
            [_revision(occurrence, revision_id="rev-a", executable="sha256:a"),
             _revision(occurrence, revision_id="rev-b", executable="sha256:b")],
            expected_pointer_version=0, declared_by="ops@bank")

    assert db.execute("SELECT count(*) FROM policy_realization_revision").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM policy_realization_current").fetchone()[0] == 0


def test_candidates_spanning_two_families_are_refused_before_any_write(db):
    with pytest.raises(ValueError, match="current for more than one thing"):
        publish_policy_realization(
            db,
            [_revision(_occurrence(), revision_id="rev-a", executable="sha256:a"),
             _revision(_occurrence(dataset="public.card_transactions"),
                       revision_id="rev-b", executable="sha256:b")],
            expected_pointer_version=0, declared_by="ops@bank")
    assert db.execute("SELECT count(*) FROM policy_realization_revision").fetchone()[0] == 0


# ══ ACCEPTANCE 2 — a non-monetary/non-per-row operand needs NO CURRENCY OCCURRENCE ═══════════════
@pytest.mark.parametrize("facts", [
    OperandFactsV2(unit="count", currency=""),
    OperandFactsV2(unit="monetary", currency="fixed:AED"),
    OperandFactsV2(),
])
def test_a_NON_PER_ROW_operand_HAS_NO_UNMET_CURRENCY_NEED(db, facts):
    """Not "the currency occurrence was excused" — it was never needed. With the one policy this
    expression declares published, nothing at all is outstanding."""
    _seed_binding(db)
    occurrence = _occurrence()
    publish_policy_realization(
        db, [_revision(occurrence, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")

    unmet = unmet_policy_kinds(
        db, expression=_expr(), operand_facts=facts,
        occurrences=PolicyOccurrenceSetV1((occurrence,)))
    assert unmet == frozenset()


def test_a_PER_ROW_MONETARY_operand_DOES_leave_a_currency_need_unmet(db):
    """The discriminator. Same expression, same published direction policy — the operand's facts
    alone move the answer, which is the whole reason occurrences beat shape."""
    _seed_binding(db)
    occurrence = _occurrence()
    publish_policy_realization(
        db, [_revision(occurrence, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")

    unmet = unmet_policy_kinds(
        db, expression=_expr(), operand_facts=OperandFactsV2(unit="monetary", currency="per_row"),
        occurrences=PolicyOccurrenceSetV1((occurrence,)))
    assert unmet == frozenset({"currency_conversion"})


def test_a_DECLARED_currency_ref_that_nothing_realizes_stays_unmet(db):
    """Declaring the reference is not supplying it. The need closes when a realization is current,
    not when someone wrote a ref into the formula."""
    _seed_binding(db)
    currency = _occurrence(role="currency_conversion", ref=FX_REF, kind="currency_conversion",
                           field="currency_conversion_ref")
    occurrences = PolicyOccurrenceSetV1((currency,))
    facts = OperandFactsV2(unit="monetary", currency="per_row")

    assert unmet_policy_kinds(db, expression=_expr(), operand_facts=facts,
                              occurrences=occurrences) == frozenset({"currency_conversion"})

    publish_policy_realization(
        db, [_revision(currency, revision_id="rev-fx", executable="sha256:rate-table")],
        expected_pointer_version=0, declared_by="ops@bank")
    assert unmet_policy_kinds(db, expression=_expr(), operand_facts=facts,
                              occurrences=occurrences) == frozenset()


def test_the_derivation_produces_no_currency_occurrence_for_a_direction_only_formula(db):
    """The same clause one level down, through the real derivation rather than a hand-built set."""
    derived = derive_policy_occurrences(
        {"body.expr": _expr()}, bound_datasets={"body.expr": DATASET}, environment_id=ENV)
    assert derived.kinds() == frozenset({"direction_sign"})


# ══ ACCEPTANCE 3 — source beats LLM, and the CONFLICT IS RETAINED ═══════════════════════════════
def test_SOURCE_BEATS_LLM_AND_THE_CONFLICT_SURVIVES_THE_WRITE(db):
    """The clause is about persistence. C-C9 computes the finding on the VERDICT, not on the winner
    — the winner is a candidate that knows nothing about the decision it went on to win — so a
    publication that wrote only the winner's own findings would drop it on the floor."""
    _seed_binding(db)
    occurrence = _occurrence()
    source = _revision(occurrence, revision_id="rev-source", executable="sha256:debit-is-D")
    proposal = _revision(occurrence, revision_id="rev-llm", executable="sha256:debit-is-DR",
                         provenance=RealizationProvenanceV1.LLM_PROPOSED)

    winner, verdict, version = publish_policy_realization(
        db, [source, proposal], expected_pointer_version=0, declared_by="ops@bank")

    assert winner.revision_id == "rev-source"
    assert verdict.outcome is AdmissibilityOutcomeV1.ADMITTED_EVIDENCE_LINKED
    assert version == 1

    stored = load_realization_revision(db, "rev-source")
    assert stored is not None
    codes = {finding.code: finding for finding in stored.conflict_findings}
    assert SOURCE_OVERRODE_LLM in codes
    # RESOLVED, and retained anyway. A realization that resolved a conflict still had one.
    assert codes[SOURCE_OVERRODE_LLM].resolved is True
    assert "rev-llm" in codes[SOURCE_OVERRODE_LLM].detail


def test_the_LOSING_PROPOSAL_IS_STORED_so_the_conflicts_reference_resolves(db):
    """A retained finding that names a revision nobody can look up records a disagreement nobody
    can inspect, which is most of what retaining it was for."""
    _seed_binding(db)
    occurrence = _occurrence()
    publish_policy_realization(
        db,
        [_revision(occurrence, revision_id="rev-source", executable="sha256:debit-is-D"),
         _revision(occurrence, revision_id="rev-llm", executable="sha256:debit-is-DR",
                   provenance=RealizationProvenanceV1.LLM_PROPOSED)],
        expected_pointer_version=0, declared_by="ops@bank")

    loser = load_realization_revision(db, "rev-llm")
    assert loser is not None
    assert loser.provenance is RealizationProvenanceV1.LLM_PROPOSED
    assert loser.is_evidence_validated is False
    # Stored, and NOT current. Being inspectable is not being authoritative.
    assert resolve_policy_occurrence(db, occurrence).revision.revision_id == "rev-source"


def test_an_AGREEING_proposal_produces_no_conflict(db):
    """Agreement is decidable on `executable_content_hash`, so there is nothing to retain — and a
    finding invented here would read as a disagreement that never happened."""
    _seed_binding(db)
    occurrence = _occurrence()
    publish_policy_realization(
        db,
        [_revision(occurrence, revision_id="rev-source", executable="sha256:same"),
         _revision(occurrence, revision_id="rev-llm", executable="sha256:same",
                   provenance=RealizationProvenanceV1.LLM_PROPOSED)],
        expected_pointer_version=0, declared_by="ops@bank")

    stored = load_realization_revision(db, "rev-source")
    assert stored is not None
    assert stored.conflict_findings == ()


def test_a_LONE_LLM_PROPOSAL_IS_USABLE_and_never_called_evidence_validated(db):
    """Invariant 16: not discarded for being a proposal, not laundered into evidence either."""
    _seed_binding(db)
    occurrence = _occurrence()
    winner, verdict, _ = publish_policy_realization(
        db, [_revision(occurrence, revision_id="rev-llm", executable="sha256:debit-is-D",
                       provenance=RealizationProvenanceV1.LLM_PROPOSED)],
        expected_pointer_version=0, declared_by="ops@bank")

    assert verdict.outcome is AdmissibilityOutcomeV1.ADMITTED_LLM_PROPOSED
    assert winner.is_evidence_validated is False
    resolved = resolve_policy_occurrence(db, occurrence)
    assert resolved.revision.provenance is RealizationProvenanceV1.LLM_PROPOSED


def test_a_conflict_the_CANDIDATE_carried_is_retained_alongside_the_decisions(db):
    """Two sources of finding, one record. A derivation-time conflict and a decision-time one are
    both true and neither displaces the other."""
    _seed_binding(db)
    occurrence = _occurrence()
    carried = ConflictFindingV1(code="AMBIGUOUS_FLAG_COLUMN",
                                detail="two columns could carry the direction flag", resolved=True)
    publish_policy_realization(
        db,
        [_revision(occurrence, revision_id="rev-source", executable="sha256:debit-is-D",
                   conflicts=(carried,)),
         _revision(occurrence, revision_id="rev-llm", executable="sha256:debit-is-DR",
                   provenance=RealizationProvenanceV1.LLM_PROPOSED)],
        expected_pointer_version=0, declared_by="ops@bank")

    stored = load_realization_revision(db, "rev-source")
    assert stored is not None
    assert {finding.code for finding in stored.conflict_findings} == {
        SOURCE_OVERRODE_LLM, "AMBIGUOUS_FLAG_COLUMN"}


# ══ ACCEPTANCE 4 — NO V2 PATH WRITES THROUGH THE MUTABLE UPSERT ═════════════════════════════════
def test_the_mutable_upsert_STILL_EXISTS_and_is_what_this_clause_names():
    """Pinned so the clause keeps meaning something. `record_eligibility` overwrites a policy in
    place through `ON CONFLICT ... DO UPDATE SET` with no version guard and no history — if that
    ever changes, this test fails and the clause below needs rewording rather than silently
    guarding nothing."""
    source = inspect.getsource(eligibility_store.record_eligibility)
    assert "ON CONFLICT (catalog_source, table_name) DO UPDATE SET" in source


def test_THE_V2_PATH_CANNOT_REACH_IT():
    """Not "does not call it today" — cannot reach it: the module imports nothing from the legacy
    store, so no call site can appear without an import appearing first in the same diff. Checked
    against the CODE, since the module explains at length what it does not write and a whole-file
    grep would read that explanation as the thing it disclaims."""
    code = _code_only(policy_store)
    assert "eligibility_store" not in code
    assert "data_agent" not in code


def test_NO_V2_WRITE_IS_AN_UNGUARDED_OVERWRITE():
    """Every write in the module is an append (`DO NOTHING`) or a version-guarded CAS. A
    `DO UPDATE SET` here would be the same defect under a new name."""
    statements = _code_only(policy_store)
    assert "DO UPDATE" not in statements.upper()
    assert "DO NOTHING" in statements.upper()


def test_THE_POINTER_REFUSES_A_STALE_WRITER_RATHER_THAN_OVERWRITING(db):
    """The behavioural half, and the actual difference from the upsert: a second writer holding a
    stale version loses, and the current answer is untouched."""
    _seed_binding(db)
    occurrence = _occurrence()
    publish_policy_realization(
        db, [_revision(occurrence, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")
    record_realization_revision(
        db, _revision(occurrence, revision_id="rev-2", executable="sha256:debit-is-DR"))

    with pytest.raises(PolicyPointerConflict, match="nothing was overwritten"):
        set_current_realization(
            db, family_key=family_key_for(occurrence), revision_id="rev-2",
            expected_pointer_version=0, declared_by="other@bank")   # stale: it is at 1
    assert resolve_policy_occurrence(db, occurrence).revision.revision_id == "rev-1"

    # And the writer who READ the current version wins.
    assert set_current_realization(
        db, family_key=family_key_for(occurrence), revision_id="rev-2",
        expected_pointer_version=1, declared_by="other@bank") == 2
    assert resolve_policy_occurrence(db, occurrence).revision.revision_id == "rev-2"


def test_THE_SUPERSEDED_REVISION_IS_STILL_THERE(db):
    """What the upsert destroys and this does not: after the pointer moves, the previous meaning is
    still readable. That is the history the mutable path has none of."""
    _seed_binding(db)
    occurrence = _occurrence()
    publish_policy_realization(
        db, [_revision(occurrence, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")
    record_realization_revision(
        db, _revision(occurrence, revision_id="rev-2", executable="sha256:debit-is-DR"))
    set_current_realization(db, family_key=family_key_for(occurrence), revision_id="rev-2",
                            expected_pointer_version=1, declared_by="ops@bank")

    superseded = load_realization_revision(db, "rev-1")
    assert superseded is not None
    assert superseded.executable_content_hash == "sha256:debit-is-D"


@pytest.mark.parametrize("table,column,key", [
    ("policy_occurrence", "policy_ref", "occurrence_hash"),
    ("policy_realization_revision", "executable_content_hash", "revision_id"),
])
def test_the_records_the_pointer_names_are_APPEND_ONLY(db, table, column, key):
    _seed_binding(db)
    occurrence = _occurrence()
    record_occurrence_set(db, PolicyOccurrenceSetV1((occurrence,)), set_id="set-1",
                          bound_input_set_revision_id="bis-s4",
                          measure_reads=_reads(occurrence))
    publish_policy_realization(
        db, [_revision(occurrence, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")

    identifier = occurrence.occurrence_hash if key == "occurrence_hash" else "rev-1"
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute(f"UPDATE {table} SET {column} = %s WHERE {key} = %s",
                   ("rewritten", identifier))


def test_a_retained_conflict_cannot_be_QUIETLY_DELETED(db):
    """Retention that a later writer can undo is not retention."""
    _seed_binding(db)
    occurrence = _occurrence()
    publish_policy_realization(
        db,
        [_revision(occurrence, revision_id="rev-source", executable="sha256:debit-is-D"),
         _revision(occurrence, revision_id="rev-llm", executable="sha256:debit-is-DR",
                   provenance=RealizationProvenanceV1.LLM_PROPOSED)],
        expected_pointer_version=0, declared_by="ops@bank")

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("DELETE FROM policy_realization_conflict WHERE revision_id = %s",
                   ("rev-source",))


# ══ occurrences hang off a binding, and corruption is surfaced ══════════════════════════════════
def test_an_occurrence_set_CANNOT_name_a_binding_that_does_not_exist(db):
    """The same rule S3 established, carried forward: an occurrence names a physical dataset, and
    one derived over no binding would name a dataset nobody bound."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        record_occurrence_set(db, PolicyOccurrenceSetV1((_occurrence(),)), set_id="set-x",
                              bound_input_set_revision_id="bis-does-not-exist",
                              measure_reads=_reads(_occurrence()))


def test_recording_an_occurrence_set_is_idempotent(db):
    _seed_binding(db)
    occurrences = PolicyOccurrenceSetV1((_occurrence(), _occurrence(expr_path="body.expr.left")))
    reads = _reads(*occurrences.occurrences)
    record_occurrence_set(db, occurrences, set_id="set-1",
                          bound_input_set_revision_id="bis-s4", measure_reads=reads)
    record_occurrence_set(db, occurrences, set_id="set-1",
                          bound_input_set_revision_id="bis-s4", measure_reads=reads)
    assert db.execute("SELECT count(*) FROM policy_occurrence").fetchone()[0] == 2


def test_a_MISFILED_revision_is_surfaced_rather_than_served(db):
    """A row that cannot reproduce the family it is filed under would be served as the current
    answer for a family it no longer belongs to."""
    _seed_binding(db)
    occurrence = _occurrence()
    record_realization_revision(
        db, _revision(occurrence, revision_id="rev-1", executable="sha256:debit-is-D"))
    # Reach past the writer: the trigger blocks UPDATE, so the misfiling is inserted directly.
    db.execute(
        "INSERT INTO policy_realization_revision (revision_id, family_key_hash, policy_kind, "
        "policy_ref, bound_dataset, environment_id, semantic_role, executable_content_hash, "
        "cas_pointer, provenance) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        ("rev-misfiled", "sha256:a-family-it-does-not-belong-to", "direction_sign", DIR_REF,
         DATASET, ENV, "direction", "sha256:x", "cas://x", "source_derived"))

    with pytest.raises(PolicyStoreCorrupt, match="no longer belongs to"):
        load_realization_revision(db, "rev-misfiled")


def test_a_pointer_naming_a_missing_revision_is_surfaced(db):
    """Unreachable through the writers (a foreign key holds), so it is reached the only way it
    could happen — and it raises rather than resolving to None."""
    _seed_binding(db)
    occurrence = _occurrence()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        set_current_realization(db, family_key=family_key_for(occurrence),
                                revision_id="rev-never-recorded", expected_pointer_version=0,
                                declared_by="ops@bank")


def test_advancing_a_pointer_must_record_WHO(db):
    """A byte-identical re-derivation REUSES the revision, so without this the act of making it
    current would leave no trace of who did it."""
    _seed_binding(db)
    occurrence = _occurrence()
    record_realization_revision(
        db, _revision(occurrence, revision_id="rev-1", executable="sha256:debit-is-D"))
    with pytest.raises(PolicyPointerConflict, match="who declared it"):
        set_current_realization(db, family_key=family_key_for(occurrence), revision_id="rev-1",
                                expected_pointer_version=0, declared_by="   ")


def test_the_migration_keys_the_pointer_on_the_WHOLE_family():
    sql = MIGRATION.read_text()
    assert "family_key_hash text PRIMARY KEY" in sql
    assert "pointer_version integer NOT NULL" in sql
    for part in ("policy_kind", "policy_ref", "bound_dataset", "environment_id", "semantic_role"):
        assert part in sql, part


# ══ C-C11's LANDING — the producer's output is admissible input to the store ════════════════════
def test_A_PRODUCER_REALIZATION_PUBLISHES_AND_RESOLVES(db):
    """The seam end to end, without an LLM call: a payload that survives C-C11's closed-taxonomy
    validation becomes a revision, publishes, and resolves — as `LLM_PROPOSED` and never as
    evidence. The producer hardcodes provenance with no parameter to turn, so this is the only
    name it can arrive under."""
    _seed_binding(db)
    occurrence = _occurrence()
    realization = realization_from_payload(
        {"policy_column_ref": f"{DATASET}.dr_cr_flag",
         "value_map": [{"semantic_value": "debit", "physical_value": "D"},
                       {"semantic_value": "credit", "physical_value": "C"}]},
        occurrence,
        revision_id="rev-produced", executable_content_hash="sha256:debit-is-D",
        cas_pointer="cas://sha256:debit-is-D")

    assert realization.provenance is RealizationProvenanceV1.LLM_PROPOSED
    winner, verdict, _ = publish_policy_realization(
        db, [realization], expected_pointer_version=0, declared_by="ops@bank")
    assert verdict.outcome is AdmissibilityOutcomeV1.ADMITTED_LLM_PROPOSED
    assert winner.is_evidence_validated is False
    assert resolve_policy_occurrence(db, occurrence).revision.revision_id == "rev-produced"


def test_A_PRODUCER_REALIZATION_LOSES_TO_A_SOURCE_with_the_conflict_retained(db):
    """The two halves meeting: C-C11 produces, C-C9 decides, S4 persists the disagreement."""
    _seed_binding(db)
    occurrence = _occurrence()
    proposed = realization_from_payload(
        {"policy_column_ref": f"{DATASET}.dr_cr_flag",
         "value_map": [{"semantic_value": "debit", "physical_value": "DR"}]},
        occurrence, revision_id="rev-produced", executable_content_hash="sha256:debit-is-DR",
        cas_pointer="cas://sha256:debit-is-DR")

    publish_policy_realization(
        db, [_revision(occurrence, revision_id="rev-source", executable="sha256:debit-is-D"),
             proposed],
        expected_pointer_version=0, declared_by="ops@bank")

    stored = load_realization_revision(db, "rev-source")
    assert stored is not None
    assert {f.code for f in stored.conflict_findings} == {SOURCE_OVERRODE_LLM}
    assert "rev-produced" in stored.conflict_findings[0].detail


def test_the_producer_REFUSES_a_physical_literal_where_a_token_belongs():
    """The closed taxonomy, restated at the S4 boundary: nothing reaches the store that C-C11's
    validator would not have accepted, because the store's entry point IS that validator."""
    with pytest.raises(ValueError, match="not one of"):
        realization_from_payload(
            {"policy_column_ref": f"{DATASET}.dr_cr_flag",
             "value_map": [{"semantic_value": "D", "physical_value": "debit"}]},
            _occurrence(), revision_id="rev-x", executable_content_hash="sha256:x",
            cas_pointer="cas://x")


# ══ C-A3c's DEFERRED GATE — provenance pinned on the occurrence ═════════════════════════════════
def test_AN_OCCURRENCE_CANNOT_BE_RECORDED_WITHOUT_ITS_MEASURE_READ(db):
    """`MeasureFact` names itself "the provenance an occurrence must pin". Refused by name, because
    the fix is to READ the column through the verified-decision seam, not to relax the write."""
    _seed_binding(db)
    with pytest.raises(OccurrenceProvenanceMissing, match="no measure read"):
        record_occurrence_set(db, PolicyOccurrenceSetV1((_occurrence(),)), set_id="set-1",
                              bound_input_set_revision_id="bis-s4", measure_reads={})


def test_the_read_is_REQUIRED_not_defaulted():
    """A default would make the unpinned call the easy one to write — the same reason S3's
    `inventory_observation_id` carries none."""
    parameter = inspect.signature(record_occurrence_set).parameters["measure_reads"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_THE_PROVENANCE_SURVIVES_THE_WRITE_WHOLE(db):
    """Which decision, over which evidence, under which policy and resolver. Without these,
    "resolved" is a word rather than something a later reader can re-derive."""
    _seed_binding(db)
    occurrence = _occurrence()
    record_occurrence_set(db, PolicyOccurrenceSetV1((occurrence,)), set_id="set-1",
                          bound_input_set_revision_id="bis-s4",
                          measure_reads=_reads(occurrence))

    stored = measure_reads_for(db, "set-1")
    currency = stored[(occurrence.occurrence_hash, "currency")]
    assert currency.value == "per_row"
    assert currency.disposition is MeasureReadDisposition.RESOLVED
    assert currency.decision_event_id == "decision-currency"
    assert currency.selected_evidence_ids == ("evidence-currency",)
    assert currency.policy_version == "field-policy-7"
    assert currency.resolver_version == "resolver-3"


def test_an_ABSENT_read_is_STORED_as_a_positive_statement(db):
    """"Nobody has decided this column's unit" is the ordinary case, since most columns are not
    measures. A missing row and a recorded absence are the two things this table keeps apart — and
    the silent version of this absence is exactly the C-A3c defect."""
    _seed_binding(db)
    occurrence = _occurrence(column="acct_id")
    record_occurrence_set(
        db, PolicyOccurrenceSetV1((occurrence,)), set_id="set-1",
        bound_input_set_revision_id="bis-s4",
        measure_reads=_reads(occurrence,
                             facts=_measure(disposition=MeasureReadDisposition.ABSENT)))

    stored = measure_reads_for(db, "set-1")
    assert set(stored) == {(occurrence.occurrence_hash, "unit"),
                           (occurrence.occurrence_hash, "currency")}
    assert stored[(occurrence.occurrence_hash, "unit")].disposition is (
        MeasureReadDisposition.ABSENT)
    assert stored[(occurrence.occurrence_hash, "unit")].value == ""


def test_TWO_DERIVATIONS_EACH_PIN_WHAT_THEY_SAW(db):
    """The catalog moves. Keying the reads on the occurrence alone would let the second derivation
    restate the first, which is the in-place overwrite this whole migration refuses elsewhere."""
    _seed_binding(db)
    occurrence = _occurrence()
    record_occurrence_set(db, PolicyOccurrenceSetV1((occurrence,)), set_id="set-before",
                          bound_input_set_revision_id="bis-s4",
                          measure_reads=_reads(occurrence,
                                               facts=_measure(unit="", currency="",
                                                              disposition=MeasureReadDisposition
                                                              .ABSENT)))
    record_occurrence_set(db, PolicyOccurrenceSetV1((occurrence,)), set_id="set-after",
                          bound_input_set_revision_id="bis-s4",
                          measure_reads=_reads(occurrence))

    before = measure_reads_for(db, "set-before")[(occurrence.occurrence_hash, "currency")]
    after = measure_reads_for(db, "set-after")[(occurrence.occurrence_hash, "currency")]
    assert before.disposition is MeasureReadDisposition.ABSENT
    assert after.value == "per_row"


def test_a_pinned_read_cannot_be_REWRITTEN(db):
    _seed_binding(db)
    occurrence = _occurrence()
    record_occurrence_set(db, PolicyOccurrenceSetV1((occurrence,)), set_id="set-1",
                          bound_input_set_revision_id="bis-s4",
                          measure_reads=_reads(occurrence))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE policy_occurrence_measure_read SET value = %s WHERE set_id = %s",
                   ("fixed:AED", "set-1"))


def test_THE_CURRENCY_NEED_IS_DECIDED_FROM_THE_VERIFIED_READ(db):
    """The C-A3c defect, closed end to end: the need calculation consumes the VERIFIED read rather
    than an `OperandFactsV2` assembled somewhere that may have degraded silently."""
    _seed_binding(db)
    occurrence = _occurrence()
    occurrences = PolicyOccurrenceSetV1((occurrence,))
    publish_policy_realization(
        db, [_revision(occurrence, revision_id="rev-1", executable="sha256:debit-is-D")],
        expected_pointer_version=0, declared_by="ops@bank")

    monetary = operand_facts_from_measure(_measure())
    assert monetary == OperandFactsV2(unit="monetary", currency="per_row")
    assert unmet_policy_kinds(db, expression=_expr(), operand_facts=monetary,
                              occurrences=occurrences) == frozenset({"currency_conversion"})

    # And an ABSENT read does not manufacture a need — it also does not hide, because the absence is
    # now a stored row rather than an empty string nobody sees.
    absent = operand_facts_from_measure(_measure(disposition=MeasureReadDisposition.ABSENT))
    assert absent == OperandFactsV2(unit="", currency="")
    assert unmet_policy_kinds(db, expression=_expr(), operand_facts=absent,
                              occurrences=occurrences) == frozenset()


def test_the_migration_stores_the_read_per_DERIVATION():
    sql = MIGRATION.read_text()
    assert "PRIMARY KEY (set_id, occurrence_hash, field)" in sql
    assert "disposition IN ('resolved', 'absent')" in sql


def _code_only(module) -> str:
    """A module's source with every docstring and comment removed.

    The same helper S3 needed, and for the same reason: this module explains at length what it does
    NOT write, and a whole-file grep reads that explanation as the thing it disclaims.
    """
    lines, inside = [], False
    for raw in inspect.getsource(module).splitlines():
        line = raw.split("#", 1)[0] if not inside else raw
        fences = line.count('"""')
        if inside:
            if fences:
                inside = False
                line = line.split('"""', 1)[1]
            else:
                continue
        elif fences == 1:
            inside = True
            line = line.split('"""', 1)[0]
        elif fences >= 2:
            line = line.split('"""')[0] + line.rsplit('"""', 1)[1]
        lines.append(line)
    return "\n".join(lines)
