"""E1a Task 6 — the concept->taxonomy cascade must carry TRANSITIVE PROVENANCE.

The behavioural fields (``additivity`` / ``temporal_role`` / ``sensitivity_floor`` /
``leakage_anchor``) are DERIVED from a column's concept, and two of them are SAFETY checks. The
derivation must therefore take BOTH its value and its strength from the ONE concept evidence record
the canonical resolution SELECTED:

* mixing them (an LLM value at a human's ``confirmed`` strength) LAUNDERS an unconfirmed guess into a
  ``taxonomy/confirmed`` safety fact that clears the ``_BEHAVIOURAL`` operational rule;
* deriving from the LLM run's in-memory ``concepts`` dict ignores the human's correction entirely.

And a human concept correction must regenerate the dependent taxonomy IN THE SAME TRANSACTION — the
cascade otherwise only ran at glossary ingest, leaving the derived safety facts carrying the OLD
concept until a re-upload that may never come.
"""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.field_resolution import is_feature_eligible, resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import derive_and_write_concept_cascade

ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))

SOURCE = "bank"
BALANCE = "bank::public.accounts.balance"
FEE = "bank::public.accounts.fee"


def _seed_graph(db):
    build_graph(db, SOURCE, [
        CanonicalRow(SOURCE, "accounts", "balance", "numeric"),
        CanonicalRow(SOURCE, "accounts", "fee", "numeric")])


def _llm_concept(db, logical_ref: str, value: str) -> str:
    """One ``llm/proposed`` concept proposal — what enrichment writes for a classified column."""
    return record_field_evidence(
        db, logical_ref=logical_ref, field_name="concept", proposed_value=value,
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        producer_ref="enrich-run", source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=logical_ref, field_name="concept", material=value))


def _cascade(db, logical_ref: str) -> None:
    derive_and_write_concept_cascade(
        db, logical_ref, producer_ref="snap-1", snapshot_id="snap-1")


def _derived(db, logical_ref: str, field_name: str):
    rows = read_active_field_evidence(db, logical_ref, field_name)
    return [e for e in rows if e.producer == EvidenceProducer.TAXONOMY.value]


def _correct(db, logical_ref, object_ref, field, action, actor, idem, **kw):
    cas = read_field_cas(db, source=SOURCE, object_ref=object_ref, field=field)
    res = apply_field_correction(
        db, source=SOURCE, object_ref=object_ref, field=field, action=action, actor=actor,
        idempotency_key=idem, expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], **kw)
    assert res["accepted"] is True, res
    return res


def _human_confirms_concept(db, logical_ref, object_ref, value: str, tag: str) -> None:
    """The REAL four-eyes correction path (propose by A, confirm by B)."""
    _correct(db, logical_ref, object_ref, "concept", "propose_override", ADMIN_A, f"{tag}-p",
             replacement_value=value)
    _correct(db, logical_ref, object_ref, "concept", "confirm_override", ADMIN_B, f"{tag}-c",
             replacement_value=value)


def test_ai_proposed_concept_derives_taxonomy_proposed_and_does_not_gate(db):
    """An LLM concept's derivation stays ``taxonomy/proposed`` — it can never clear _BEHAVIOURAL."""
    _seed_graph(db)
    _llm_concept(db, BALANCE, "monetary_stock")

    _cascade(db, BALANCE)

    additivity = _derived(db, BALANCE, "additivity")
    assert len(additivity) == 1
    assert additivity[0].strength == AssertionStrength.PROPOSED.value, (
        "an AI-proposed concept derived a stronger-than-proposed safety fact — strength propagation "
        f"(spec §3.2) is broken: {additivity[0]}")
    assert additivity[0].proposed_value == "semi_additive"

    resolve_and_project(db, source=SOURCE, logical_refs=[BALANCE])
    assert is_feature_eligible(db, BALANCE, "additivity") is False, (
        "a taxonomy/proposed derivation cleared the behavioural gate — an AI guess became "
        "operational")
    assert is_feature_eligible(db, BALANCE, "leakage_anchor") is False


def test_cascade_never_launders_a_later_llm_value_into_the_confirmed_strength(db):
    """THE load-bearing test. A human confirmed ``monetary_stock``; a LATER LLM run proposes
    ``default_flag`` (a leakage anchor). The cascade must derive from the RESOLVED human record —
    value AND strength together — never the LLM's value at the human's ``confirmed`` strength."""
    _seed_graph(db)
    _llm_concept(db, BALANCE, "monetary_stock")
    _cascade(db, BALANCE)
    resolve_and_project(db, source=SOURCE, logical_refs=[BALANCE])
    _human_confirms_concept(db, BALANCE, "public.accounts.balance", "monetary_stock", "h1")

    # A later enrichment run reclassifies the column to a DIFFERENT concept, at llm/proposed.
    _llm_concept(db, BALANCE, "default_flag")
    _cascade(db, BALANCE)

    additivity = _derived(db, BALANCE, "additivity")
    assert len(additivity) == 1
    assert additivity[0].strength == AssertionStrength.CONFIRMED.value
    assert additivity[0].proposed_value == "semi_additive", (
        "the derivation took its VALUE from the LLM's `default_flag` (which declares no additivity "
        "at all) while taking its STRENGTH from the human record — an unconfirmed value laundered "
        f"into a confirmed safety fact: {additivity[0]}")
    leakage = _derived(db, BALANCE, "leakage_anchor")
    assert len(leakage) == 1
    assert leakage[0].proposed_value is False, (
        "the LLM's `default_flag` value was derived as a CONFIRMED leakage anchor — a safety fact "
        f"the human never asserted: {leakage[0]}")
    assert leakage[0].strength == AssertionStrength.CONFIRMED.value


def test_human_concept_correction_regenerates_the_taxonomy_in_the_same_transaction(db):
    """A governed concept correction must recompute the dependent taxonomy IMMEDIATELY — not leave
    it stale until (maybe) a later re-upload."""
    _seed_graph(db)
    _llm_concept(db, BALANCE, "monetary_flow")           # additive
    _cascade(db, BALANCE)
    resolve_and_project(db, source=SOURCE, logical_refs=[BALANCE])
    assert _derived(db, BALANCE, "additivity")[0].proposed_value == "additive"

    _human_confirms_concept(db, BALANCE, "public.accounts.balance", "monetary_stock", "h1")

    additivity = _derived(db, BALANCE, "additivity")
    assert len(additivity) == 1
    assert additivity[0].proposed_value == "semi_additive", (
        "the concept correction left the DERIVED additivity on the superseded concept — the "
        f"cascade did not run on the correction path: {additivity[0]}")
    assert additivity[0].strength == AssertionStrength.CONFIRMED.value
    # The derived DECISION must move too, else the recompute is inert to every operational reader.
    assert is_feature_eligible(db, BALANCE, "additivity") is True
    assert db.execute(
        "SELECT additivity FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (SOURCE, "public.accounts.balance")).fetchone()[0] == "semi_additive"


def test_derived_rows_carry_queryable_root_links_to_their_parent_concept(db):
    """Two columns whose concept has the SAME value but a different ORIGIN must be distinguishable
    from the derived row alone — value + strength cannot tell them apart, the root link can."""
    _seed_graph(db)
    llm_evidence_id = _llm_concept(db, BALANCE, "monetary_stock")
    _cascade(db, BALANCE)

    derived = _derived(db, BALANCE, "additivity")[0]
    assert derived.producer_item_ref == llm_evidence_id
    assert derived.evidence_spans == (llm_evidence_id,)

    # Same concept VALUE on a second column, but human-confirmed rather than AI-proposed.
    _llm_concept(db, FEE, "monetary_flow")
    _cascade(db, FEE)
    resolve_and_project(db, source=SOURCE, logical_refs=[FEE])
    _human_confirms_concept(db, FEE, "public.accounts.fee", "monetary_stock", "h2")

    # The chain is queryable: join the derived row's root link back to the concept evidence.
    origins = dict(db.execute(
        "SELECT d.logical_ref, r.producer FROM field_evidence d "
        "JOIN field_evidence r ON r.evidence_id = d.producer_item_ref "
        "WHERE d.field_name = 'additivity' AND d.producer = 'taxonomy' AND d.lifecycle = 'active' "
        "AND r.field_name = 'concept'").fetchall())
    assert origins == {BALANCE: EvidenceProducer.LLM.value, FEE: EvidenceProducer.HUMAN.value}, (
        "two same-valued concepts of different origin produced indistinguishable derived rows — "
        f"the root link is missing or wrong: {origins}")
