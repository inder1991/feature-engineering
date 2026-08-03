"""Joint Task 4 (g) — GOLDEN payloads: the exact bytes the classifier and the critic receive.

Six witness columns, pinned byte-for-byte under the new purpose adapters. Five carry the names the
existing suites already reason about (`counter_party_bic`, `counter_party_cif_id`,
`actual_counter_party_amt`, `tran_crncy`, `sol_desc` — see `attest/test_representation.py`,
`test_column_readiness.py`, `test_semantic_binding_pipeline.py`); `pstd_date` exists NOWHERE in the
repo (the plan review found a single hit, in a plan document), so it is SYNTHESIZED here from the
FTR file's own column shape: a posted-date temporal column with an FTR-style definition, a declared
type and the same BIAN/process facets its neighbours carry.

Why bytes and not fields: a golden's job is to make a payload change VISIBLE in review. Every
assertion above this file tests a property; this one tests the artefact. When it fails, the diff is
the change — read it, decide whether it was intended, and re-pin deliberately (the goldens live in
`fixtures/pass_a_golden_payloads.json`, one reviewable file).

No live LLM call: the payload builders are pure.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from featuregen.overlay.upload import enrich
from featuregen.overlay.upload import semantic_context as sc
from featuregen.overlay.upload.attest import concept_critic as critic
from featuregen.overlay.upload.attest.representation import shape_conflicts
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.glossary_reader import GlossaryRecord

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "pass_a_golden_payloads.json"

_SOURCE = "ftr"
_TABLE = "comp_fin_tran"
_SCHEMA = "dpl_eib_compliance"

#: The six witnesses. `pstd_date` is the SYNTHESIZED one (see the module docstring); the rest keep
#: the shape the FTR export gives them — an all-caps physical name, a curated business definition,
#: a declared SQL type, and the BIAN/process facets.
_COLUMNS: dict[str, dict] = {
    "counter_party_bic": {
        "term_name": "Counterparty BIC",
        "definition": "The SWIFT Business Identifier Code of the counterparty bank on the "
                      "transaction.",
        "domain": "Compliance",
        "declared_type": "varchar(11)",
        "term_type": "Attribute",
        "bian_path": "Party Reference Data Directory > Party Routing Profile",
        "fibo_path": "fibo-fbc-fi:BankIdentifier",
        "process_path": "Payments > Cross Border > Beneficiary Routing",
        "synonyms": ("SWIFT Code", "BIC"),
        "related_terms": ("Sender BIC", "Beneficiary Bank"),
        "concept": "bank_bic",
    },
    "counter_party_cif_id": {
        "term_name": "Counterparty CIF Identifier",
        "definition": "The bank's own customer information file identifier for the counterparty.",
        "domain": "Party",
        "declared_type": "varchar(20)",
        "term_type": "Dimension",
        "bian_path": "Party Reference Data Directory > Customer Identification",
        "fibo_path": "fibo-fnd-pty:PartyIdentifier",
        "process_path": "Onboarding > KYC > Customer Identification",
        "synonyms": ("CIF", "Customer Number"),
        "related_terms": ("Customer", "Counterparty"),
        "concept": "customer_id",
    },
    "actual_counter_party_amt": {
        "term_name": "Actual Counterparty Amount",
        "definition": "The settled amount transferred to the counterparty on this transaction.",
        "domain": "Payments",
        "declared_type": "decimal(18,2)",
        "term_type": "Measure",
        "bian_path": "Payment Execution > Payment Order",
        "fibo_path": "fibo-fnd-acc-cur:MonetaryAmount",
        "process_path": "Payments > Settlement > Posting",
        "synonyms": ("Settled Amount",),
        "related_terms": ("Transaction Currency",),
        "concept": "monetary_flow",
    },
    "tran_crncy": {
        "term_name": "Transaction Currency",
        "definition": "The ISO-4217 currency the transaction amount is denominated in.",
        "domain": "Payments",
        "declared_type": "char(3)",
        "term_type": "Attribute",
        "bian_path": "Payment Execution > Payment Order",
        "fibo_path": "fibo-fnd-acc-cur:Currency",
        "process_path": "Payments > Settlement > Posting",
        "synonyms": ("Currency Code",),
        "related_terms": ("Actual Counterparty Amount",),
        "concept": "currency_code",
    },
    "pstd_date": {           # SYNTHESIZED — no repo witness exists (plan review, C Task 4)
        "term_name": "Posted Date",
        "definition": "The business date on which the transaction was posted to the ledger.",
        "domain": "Payments",
        "declared_type": "date",
        "term_type": "Attribute",
        "bian_path": "Payment Execution > Payment Order",
        "fibo_path": "fibo-fnd-dt-fd:Date",
        "process_path": "Payments > Settlement > Posting",
        "synonyms": ("Posting Date",),
        "related_terms": ("Value Date",),
        "concept": "booking_date",
    },
    "sol_desc": {
        "term_name": "Branch Description",
        "definition": "The free-text description of the service outlet (branch) that booked the "
                      "transaction.",
        "domain": "Party",
        "declared_type": "varchar(200)",
        "term_type": "Attribute",
        "bian_path": "Party Reference Data Directory > Service Outlet",
        "fibo_path": "fibo-fnd-org-fm:Branch",
        "process_path": "Onboarding > Servicing > Branch Administration",
        "synonyms": ("Branch Name",),
        "related_terms": ("Branch",),
        "concept": "branch_id",     # the live conflation defect — a DESCRIPTION read as an id
    },
}

#: The table-grain context every witness shares (the previous run's resolution at the TABLE ref).
_TABLE_CONTEXT = (
    sc.SemanticValueV1(field_name="primary_entity", value="counterparty", evidence=(),
                       resolution_status="current"),
    sc.SemanticValueV1(field_name="table_role", value="event_fact", evidence=(),
                       resolution_status="current"),
)


def _row(column: str) -> CanonicalRow:
    return CanonicalRow(_SOURCE, _TABLE, column, "unknown")


def _record(column: str) -> GlossaryRecord:
    spec = _COLUMNS[column]
    return GlossaryRecord(
        logical_ref=f"{_SOURCE}::{_SCHEMA}.{_TABLE}.{column}",
        term_name=spec["term_name"],
        definition=spec["definition"],
        domain=spec["domain"],
        synonyms=spec["synonyms"],
        bian_path=spec["bian_path"],
        fibo_path=spec["fibo_path"],
        term_type=spec["term_type"],
        process_path=spec["process_path"],
        related_terms=spec["related_terms"],
        schema=_SCHEMA,
        physical_fqn=f"{_SCHEMA}.{_TABLE}.{column}",
        declared_type=spec["declared_type"],
    )


def _bundle(column: str) -> sc.SemanticContextBundleV1:
    rows = [_row(c) for c in _COLUMNS]
    anchor = next(r for r in rows if r.column == column)
    return sc.bundle_from_upload(anchor, glossary_record=_record(column), cohort=rows,
                                 roles=enrich._ENRICHMENT_ROLES, table_context=_TABLE_CONTEXT)


def _classifier_payload(column: str) -> dict:
    return enrich._classifier_payload(_row(column), _record(column), _bundle(column))


def _critic_payload(column: str) -> dict:
    spec = _COLUMNS[column]
    meta = enrich._concept_metadata(_row(column), _record(column))
    context = enrich._definition_egress_guard(
        sc.for_critic(_bundle(column), extra=meta), meta, curated=True)
    item = critic.ConceptCriticItemV1(
        logical_ref=f"{_SOURCE}::{_SCHEMA}.{_TABLE}.{column}",
        column_name=column,
        declared_type=spec["declared_type"],
        definition=meta.get("business_definition"),
        proposed_concept=spec["concept"],
        context=context,
    )
    conflicts = shape_conflicts(column, spec["declared_type"], item.definition, spec["concept"])
    return critic._item_payload(item, conflicts)


def _actual() -> dict:
    return {column: {"classifier": _classifier_payload(column),
                     "critic": _critic_payload(column)}
            for column in sorted(_COLUMNS)}


def _golden() -> dict:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("column", sorted(_COLUMNS))
def test_the_classifier_payload_matches_its_golden(column: str) -> None:
    assert json.dumps(_classifier_payload(column), sort_keys=True, indent=2) == \
        json.dumps(_golden()[column]["classifier"], sort_keys=True, indent=2)


@pytest.mark.parametrize("column", sorted(_COLUMNS))
def test_the_critic_payload_matches_its_golden(column: str) -> None:
    assert json.dumps(_critic_payload(column), sort_keys=True, indent=2) == \
        json.dumps(_golden()[column]["critic"], sort_keys=True, indent=2)


def test_the_golden_file_covers_exactly_the_six_witnesses() -> None:
    assert set(_golden()) == set(_COLUMNS)
    assert "pstd_date" in _golden(), "the synthesized temporal witness must be pinned too"


@pytest.mark.parametrize("column", sorted(_COLUMNS))
def test_every_golden_payload_passes_the_per_item_egress_contract(column: str) -> None:
    """A pinned payload that could not actually egress would be a golden of a fiction."""
    from featuregen.overlay.upload.enrich_llm import _item_egress_ok, _redact_free_text_meta
    payload = _golden()[column]["classifier"]
    assert _item_egress_ok(payload), sorted(payload)
    scrubbed, _spans, _audits, _version = _redact_free_text_meta(dict(payload))
    assert scrubbed is not None, "the classifier golden must not fail the egress gate closed"
    # The critic goes through the SINGLE-call path, so the top-level classification gate is what
    # applies to it — every key it carries must be classified.
    critic_payload = _golden()[column]["critic"]
    scrubbed, _spans, _audits, _version = _redact_free_text_meta(dict(critic_payload))
    assert scrubbed is not None, sorted(critic_payload)


def test_the_description_witness_still_carries_its_deterministic_refutation() -> None:
    """`sol_desc` classified `branch_id` is the live conflation defect the critic exists for: the
    golden must show the deterministic conflict reaching the payload, not an empty list."""
    assert _golden()["sol_desc"]["critic"]["shape_conflicts"] == [
        "name_or_description_not_identifier"]


def test_the_monetary_and_temporal_witnesses_are_shape_clean() -> None:
    """A correct assignment in a newly-criticised group must NOT be refuted — the misfire risk item
    (d) exists to bound."""
    assert _golden()["actual_counter_party_amt"]["critic"]["shape_conflicts"] == []
    assert _golden()["pstd_date"]["critic"]["shape_conflicts"] == []
