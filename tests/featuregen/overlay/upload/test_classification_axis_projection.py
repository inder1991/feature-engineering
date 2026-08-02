"""D13.1/D13.2 — the fine-grained classification axes reach `graph_node` and survive a re-upload.

`bian_path` / `process_path` have been captured as SOURCE `field_evidence` since the richness
Step-6b work, but with no field policy `resolve_and_project` skipped them (`policy_for` returned
``None``), so nothing they said could reach a flat column — and the facet mechanism requires literal
columns. `sub_domain` is the NEW D13.2 axis: an `llm/proposed` value must be VISIBLE (labeled), must
NOT be load-bearing, and must never overwrite the coarse source `domain`.

The re-upload guard is the one that matters operationally: `build_graph` DELETEs and recreates every
node with NULL display columns on every ingest, so a projection that is not re-derived from the
surviving evidence is a projection that silently disappears on the next upload.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import InfluenceTier
from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_policies import policy_for
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.glossary_reader import GlossaryRecord
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import _write_glossary_source_evidence

_SOURCE = "ftr"
_TABLE = "ftr_txn"
_COLUMN = "counter_party_bic"
_SCHEMA = "dpl_core"
_REF = f"{_SOURCE}::{_SCHEMA}.{_TABLE}.{_COLUMN}"
_FLAT = f"public.{_TABLE}.{_COLUMN}"


def _rows() -> list[CanonicalRow]:
    return [CanonicalRow(_SOURCE, _TABLE, _COLUMN, "unknown"),
            CanonicalRow(_SOURCE, _TABLE, "pstd_date", "unknown")]


def _record() -> GlossaryRecord:
    return GlossaryRecord(
        logical_ref=_REF,
        term_name="Counterparty BIC",
        definition="The SWIFT BIC of the counterparty bank.",
        domain="Compliance",
        bian_path="Party Reference Data Directory > Party Routing Profile",
        process_path="Payments > Cross Border > Beneficiary Routing",
        term_type="attribute",
        schema=_SCHEMA,
        declared_type="varchar(11)",
    )


def _build(db) -> None:
    rows = _rows()
    schemas = {f"public.{r.table}.{r.column}": _SCHEMA for r in rows}
    schemas.update({f"public.{r.table}": _SCHEMA for r in rows})
    build_graph(db, _SOURCE, rows, concepts={}, schemas=schemas)


def _node(db) -> tuple:
    return db.execute(
        "SELECT bian_path, process_path, sub_domain, domain FROM graph_node "
        "WHERE catalog_source = %s AND lower(object_ref) = %s",
        (_SOURCE, _FLAT)).fetchone()


def test_source_taxonomy_axes_project_onto_the_column_node(db) -> None:
    _build(db)
    _write_glossary_source_evidence(db, logical_ref=_REF, rec=_record(), snapshot_id="snap-1")
    assert _node(db)[:3] == (None, None, None)   # evidence alone never projects

    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])

    bian, process, sub_domain, domain = _node(db)
    assert bian == "Party Reference Data Directory > Party Routing Profile"
    assert process == "Payments > Cross Border > Beneficiary Routing"
    assert sub_domain is None            # nothing proposed one yet — honestly absent
    assert domain == "Compliance"        # the coarse source axis is untouched


def test_the_axes_survive_a_re_upload_that_recreates_every_node(db) -> None:
    _build(db)
    _write_glossary_source_evidence(db, logical_ref=_REF, rec=_record(), snapshot_id="snap-1")
    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])
    assert _node(db)[0] is not None

    _build(db)                                   # the re-upload: nodes deleted + recreated
    assert _node(db)[:3] == (None, None, None)
    _write_glossary_source_evidence(db, logical_ref=_REF, rec=_record(), snapshot_id="snap-2")
    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])

    bian, process, _sub, _domain = _node(db)
    assert bian == "Party Reference Data Directory > Party Routing Profile"
    assert process == "Payments > Cross Border > Beneficiary Routing"


def test_an_llm_sub_domain_is_shown_beside_the_source_domain_and_never_load_bearing(db) -> None:
    _build(db)
    _write_glossary_source_evidence(db, logical_ref=_REF, rec=_record(), snapshot_id="snap-1")
    record_field_evidence(
        db, logical_ref=_REF, field_name="sub_domain",
        proposed_value="Correspondent Banking", producer=EvidenceProducer.LLM,
        strength=AssertionStrength.PROPOSED, producer_ref="overlay-enrichment",
        source_snapshot_id="snap-1", input_hash="h" * 64)

    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])

    _bian, _process, sub_domain, domain = _node(db)
    assert sub_domain == "Correspondent Banking"    # visible, labeled llm_proposed downstream
    assert domain == "Compliance"                   # NEVER overwritten by the finer axis
    decision = db.execute(
        "SELECT load_bearing_value_hash FROM field_decision_event "
        "WHERE logical_ref = %s AND field_name = 'sub_domain' "
        "ORDER BY created_at DESC, decision_event_id DESC LIMIT 1", (_REF,)).fetchone()
    assert decision is not None and decision[0] is None   # an LLM proposal is never load-bearing


@pytest.mark.parametrize("field", ["bian_path", "process_path", "sub_domain"])
def test_every_new_axis_is_recommendation_tier(field: str) -> None:
    policy = policy_for(field)
    assert policy is not None, field
    assert policy.influence_max is InfluenceTier.RECOMMENDATION, field


def test_sub_domain_carries_the_same_policy_shape_as_domain() -> None:
    """D13.2: "recommendation/display tier exactly like `domain`" — including human-editability, so
    the existing four-eyes propose/confirm flow can correct an AI proposal."""
    assert policy_for("sub_domain") == policy_for("domain")
    assert policy_for("sub_domain").human_editable is True
