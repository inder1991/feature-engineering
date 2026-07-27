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

Two more properties the final review added:

* a concept PENDING REVALIDATION derives nothing (the ``_CONCEPT`` policy's RECOMMENDATION ceiling
  short-circuits ``resolve_field_authority`` before its disqualifier check, so the flag would
  otherwise be inert and the cascade would keep re-deriving ``taxonomy/confirmed`` safety facts from
  a confirmation the material change invalidated);
* the INGEST cascade retires the DECISION of a derived field it no longer emits — the projection
  skips an evidence-less field, so the prior load-bearing decision would stay the latest and keep a
  retired value feature-eligible.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_ftr_adapter import _HDR, _row

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import (
    field_input_hash,
    read_active_field_evidence,
    record_field_evidence,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.field_resolution import is_feature_eligible, resolve_and_project
from featuregen.overlay.upload.field_revalidation import flag_pending_revalidation
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import (
    derive_and_write_concept_cascade,
    ingest_upload,
    resolve_concept_evidence,
)
from featuregen.overlay.upload.object_ref import normalize_ref

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


def _correct(db, logical_ref, object_ref, field, action, actor, idem, *, source=SOURCE, **kw):
    cas = read_field_cas(db, source=source, object_ref=object_ref, field=field)
    res = apply_field_correction(
        db, source=source, object_ref=object_ref, field=field, action=action, actor=actor,
        idempotency_key=idem, expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], **kw)
    assert res["accepted"] is True, res
    return res


def _human_confirms_concept(db, logical_ref, object_ref, value: str, tag: str,
                            *, source=SOURCE) -> None:
    """The REAL four-eyes correction path (propose by A, confirm by B)."""
    _correct(db, logical_ref, object_ref, "concept", "propose_override", ADMIN_A, f"{tag}-p",
             source=source, replacement_value=value)
    _correct(db, logical_ref, object_ref, "concept", "confirm_override", ADMIN_B, f"{tag}-c",
             source=source, replacement_value=value)


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


# ── Final review F2: a concept PENDING REVALIDATION must derive nothing ───────────────────────────
# `_CONCEPT` is a RECOMMENDATION-ceiling policy with no disqualifiers of its own, so
# `resolve_field_authority` returns at the ceiling check BEFORE it would honour the flag. The cascade
# is what gives `concept` its operational REACH (a `taxonomy/confirmed` derivation clears
# `_BEHAVIOURAL`), so the flag has to be honoured HERE — else a confirmation the material change
# invalidated keeps re-deriving operational safety facts on every subsequent ingest.


def test_concept_pending_revalidation_derives_no_taxonomy(db):
    """A human-confirmed concept whose column MATERIAL then changed is PENDING revalidation — the
    cascade must derive nothing from it, and the derived behavioural facts must stop gating."""
    _seed_graph(db)
    _llm_concept(db, BALANCE, "monetary_stock")
    _cascade(db, BALANCE)
    resolve_and_project(db, source=SOURCE, logical_refs=[BALANCE])
    _human_confirms_concept(db, BALANCE, "public.accounts.balance", "monetary_stock", "h1")
    assert is_feature_eligible(db, BALANCE, "additivity") is True

    flag_pending_revalidation(
        db, logical_ref=BALANCE, field_name="concept",
        reason="source re-upload changed the column's material (definition/type); the human "
               "confirmation must be revalidated",
        source_snapshot_id="snap-2", now=None)

    assert resolve_concept_evidence(db, BALANCE) is None, (
        "the concept resolution ignored an ACTIVE disqualifier — a confirmation pending "
        "revalidation still selected a record to derive operational taxonomy from")
    _cascade(db, BALANCE)                       # the next ingest's cascade
    assert _derived(db, BALANCE, "additivity") == []
    assert _derived(db, BALANCE, "temporal_role") == []


# ── Final review F1: the INGEST cascade must retire the DECISION of a field it no longer emits ────
# `resolve_and_project` iterates only fields with ACTIVE evidence, so a derived field the cascade
# staled keeps its PRIOR load-bearing decision (and stays feature-eligible) unless the decision is
# retired first — exactly what the parser/technical paths already do for their dropped fields.

_NAME_OBJECT_REF = "public.comp_fin_tran.cust_name"
# Its OWN source: this test ingests an FTR GLOSSARY upload, and the MF-6 source-kind guard holds an
# FTR upload onto a source another test already created as schema-less technical. `SOURCE` ("bank")
# is generic enough to collide in a full-suite run, so the FTR path gets a distinct name.
_FTR_SOURCE = "bank_ftr_cascade"
_NAME_REF = normalize_ref(_FTR_SOURCE, "DPL_EIB_COMPLIANCE", "COMP_FIN_TRAN", "CUST_NAME")


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _uploader() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _quiet_client() -> FakeLLM:
    """An LLM that proposes nothing — the concept in play is the HUMAN's confirmation."""
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": []}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": []}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
    })


def _ingest_ftr(db, definition: str, now: datetime) -> None:
    upload = to_glossary_upload(
        read_ftr_glossary(_HDR + _row(definition=definition), source=_FTR_SOURCE))
    res = ingest_upload(db, _FTR_SOURCE, upload.rows, actor=_uploader(), now=now,
                        client=_quiet_client(), glossary=upload)
    assert res.status == "ingested"


def test_ingest_retires_the_decision_of_a_derived_field_the_cascade_dropped(db):
    """END TO END on the INGEST path: a human-confirmed concept makes `additivity` operational; a
    re-upload changes the column's MATERIAL (flagging the confirmation pending revalidation), and the
    NEXT ingest's cascade can no longer derive anything. Its evidence is staled — and its DECISION
    must be retired with it, else `is_feature_eligible` keeps serving the withdrawn value."""
    _seal()
    # The correction path stamps its decisions with the REAL clock, so the later ingests must be
    # stamped after it — the decision log is ordered by time, not by call order.
    started = datetime.now(UTC)
    _ingest_ftr(db, "Registered legal name of the counterparty.", started)
    _llm_concept(db, _NAME_REF, "monetary_flow")
    _cascade(db, _NAME_REF)
    resolve_and_project(db, source=_FTR_SOURCE, logical_refs=[_NAME_REF])
    _human_confirms_concept(db, _NAME_REF, _NAME_OBJECT_REF, "monetary_stock", "h1",
                            source=_FTR_SOURCE)
    assert is_feature_eligible(db, _NAME_REF, "additivity") is True

    # A re-upload with a CHANGED definition = a material change -> the confirmation is flagged.
    _ingest_ftr(db, "The counterparty's registered trading name.", started + timedelta(hours=1))
    # ...and the NEXT ingest's cascade finds no concept it may derive from.
    _ingest_ftr(db, "The counterparty's registered trading name.", started + timedelta(hours=2))

    assert _derived(db, _NAME_REF, "additivity") == []
    assert is_feature_eligible(db, _NAME_REF, "additivity") is False, (
        "the cascade staled the derived additivity EVIDENCE but its prior load-bearing DECISION "
        "stayed the latest — the ingest path never retired it")
    assert db.execute(
        "SELECT additivity FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (_FTR_SOURCE, _NAME_OBJECT_REF)).fetchone()[0] is None
