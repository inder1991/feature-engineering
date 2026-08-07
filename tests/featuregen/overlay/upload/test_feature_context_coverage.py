"""Every semantic field the platform produces must be a DELIBERATE decision at the feature payload.

The leak this exists to catch: `semantic_context.for_feature_generation` is a hand-written dict
literal. Migration 1051 added `sub_domain` / `bian_path` / `process_path`, an LLM stage ran on every
ingest to fill them, and NOTHING failed when the payload did not carry them — so the output of that
stage never reached the consumer it was built for. `table_role` / `event_or_snapshot` were the same
story one grain up. Five fields, silently, for as long as nobody happened to diff two lists by hand.

A test that listed today's fields and asserted they are present would pass forever and catch
nothing. So BOTH sides are DERIVED from the code, and the comparison is on VALUES, not key names.

**Side A — what the platform produces**, the union of six registries, none of them written here:

* `field_policies._POLICIES` — the field names that have a resolution policy. This is the EARLIEST
  chokepoint: `field_resolution` skips a field `policy_for` does not know, so an enrichment field
  without an entry here can never be resolved at all. Every one of the five leaked fields had to
  pass through it.
* `field_resolution._DISPLAY_COLUMN` — the field -> `graph_node` column projection. A field must be
  here to reach a flat display column, which is where `semantic_context.bundle_from_store` READS
  the column and table anchors from.
* `semantic_context._DISPLAY_FIELDS` / `._OPERATIONAL_FIELDS` — what the bundle carries at column
  grain, as `resolved_semantics`.
* `semantic_context._GLOSSARY_SOURCE_FIELDS` / `._TECHNICAL_SOURCE_FIELDS` — what the bundle
  carries as `source_semantics`, i.e. the uploader's own declared values. `bian_path` and
  `process_path` are members: the leak class is not "the LLM's answers", it is "anything the
  platform knows".

**Side B — what feature generation receives**: `for_feature_generation` CALLED on a bundle whose
every side-A field carries a distinct sentinel string, with the whole returned payload flattened.
Not `.keys()`: a key present but always null (`out["sub_domain"] = _value_of(bundle, "subdomain")`,
one typo) carries nothing while passing a key-set assertion. Comparing values makes the test say
what it means — this field's value REACHES the payload.

A field that is in neither the payload nor one of the two accounting maps below therefore FAILS by
default, which is the point: the next enrichment field cannot be added without somebody deciding,
in writing, whether feature generation gets it.

The two maps are kept apart on purpose. `DELIBERATELY_OMITTED` is a decision that stands.
`UNCARRIED_GAPS` is a FINDING — a field that arguably should be carried and is not — recorded so it
is visible and countable rather than dissolved into the first list.

BLIND SPOT, stated rather than papered over: `axis_projection` writes four `graph_node` columns
directly, without a policy or a `_DISPLAY_COLUMN` entry. Three of them (`entity`, `additivity`,
`party_role`) are side-A members by another route; the fourth, `sensitivity_display`, is not covered
by any registry this test can derive. It is a rendering label for `visible_requires` and has no
business in a prompt, but a future axis added the same way would not be caught here.

The private imports are deliberate: these are the module-internal registries that ARE the contract,
and a test that read a public restatement of them would be guarding the restatement.
"""
from __future__ import annotations

from featuregen.overlay.upload.bridge_cardinality import Cardinality
from featuregen.overlay.upload.enrich_llm import (
    _FEATURE_COLUMN_DEFINITION_KEYS,
    _FEATURE_COLUMN_DICT_LIST_KEYS,
    _FEATURE_COLUMN_FACT_KEYS,
    _FEATURE_COLUMN_IDENTITY_KEYS,
    _FEATURE_COLUMN_MAPPING_KEYS,
    _FEATURE_COLUMN_OBJECT_KEYS,
    _FEATURE_COLUMN_PROSE_KEYS,
    _FEATURE_COLUMN_TOKEN_LIST_KEYS,
    _FEATURE_FACT_SUBKEYS,
)
from featuregen.overlay.upload.field_policies import _POLICIES
from featuregen.overlay.upload.field_resolution import _DISPLAY_COLUMN
from featuregen.overlay.upload.semantic_context import (
    _DISPLAY_FIELDS,
    _GLOSSARY_SOURCE_FIELDS,
    _OPERATIONAL_FIELDS,
    _TECHNICAL_SOURCE_FIELDS,
    CONTRACT_VERSION,
    UNRESOLVED_PENDING_REVIEW,
    DirectionalRealizationContextV1,
    EvidenceAuthorityV1,
    IdentifierNamespaceV1,
    RelationshipContextV1,
    RelationshipKind,
    SemanticContextBundleV1,
    SemanticValueV1,
    for_feature_generation,
)

# ── the accounting: every side-A field the payload does not carry, and why ───────────────────────

#: Fields the feature payload deliberately DOES NOT carry per column, each with the reason a
#: reviewer can argue with. Four of these DO reach feature generation — at TABLE grain, through
#: `feature_assist._table_context` / `_profile_advisories` — which is why "not in the column payload"
#: is not the same claim as "never reaches the model".
DELIBERATELY_OMITTED: dict[str, str] = {
    "primary_entity": (
        "TABLE grain. Carried by `feature_assist._table_context` as `primary_entity` on the table "
        "block; repeating it on every column of the table would be the same fact N times."),
    "authority_role": (
        "TABLE grain. Carried by `feature_assist._profile_advisories` on the table block, under the "
        "Release-A profile flag `FEATUREGEN_DATASET_PROFILES` (off ⟹ the block is byte-identical to "
        "the pre-profile shape, which is the flag's whole contract)."),
    "temporal_storage_model": (
        "TABLE grain. Carried by `feature_assist._profile_advisories` beside `authority_role`, "
        "under the same flag."),
    "business_context": (
        "TABLE grain. Carried by `feature_assist._profile_advisories` under the same flag, and "
        "definition-graded on egress as `_TABLE_CONTEXT_DEFINITION_KEYS`."),
    "physical_fqn": (
        "The column's identity in the SOURCE's spelling. The payload publishes exactly ONE "
        "identity, `object_ref`, and it must be the public-flattened graph ref or grounding fails "
        "to match what the model copies back (`feature_assist._context_v4_column`). A second "
        "spelling of the same thing is a grounding hazard, not extra context."),
    "sensitivity": (
        "The read-scope TAG, not a semantic property: migration 1032 derives the GENERATED "
        "`visible_requires` enforcement column from it, and read scope is applied BEFORE this "
        "payload exists — a column the caller may not see never enters the menu. Sending the "
        "enforcement axis into a prompt can only invite the model to reason about it."),
    "term_type": (
        "Classifies the GLOSSARY ENTRY (business term vs data element), not the data. The "
        "platform's own `concept` is the classification feature generation reasons with."),
    "logical_representation": (
        "Carried, under the bundle's own name for it. `semantic_context._EVIDENCE_FIELD` and "
        "`._DECISION_GOVERNED` both map the bundle field `data_type` onto this evidence/decision "
        "field, so its evidence and its governed|hint verdict ride the `data_type` wrapper."),
    "semantic_type": (
        "Decision-only — no `_DISPLAY_COLUMN` entry, so it reaches no flat column and no bundle "
        "field. `field_resolution` keeps it decision-only deliberately, so two fields do not "
        "clobber one `logical_type_decision_id` link column."),
    "temporal_role": (
        "Decision-only — no `_DISPLAY_COLUMN` entry, nothing to carry. The payload's temporal axis "
        "is the governed `is_as_of` fact."),
    "leakage_anchor": (
        "Decision-only — no `_DISPLAY_COLUMN` entry. Leakage is enforced by `templates.py`'s "
        "registry check against the CONCEPT, never by a value in a prompt."),
    "feature_role": (
        "Decision-only — no `_DISPLAY_COLUMN` entry and no producer; a policy with no emitter has "
        "no value to leak."),
}

#: NOT decisions — FINDINGS. Each is a field the platform holds and feature generation arguably
#: should see, left uncarried at this commit. They are listed apart from `DELIBERATELY_OMITTED` so
#: nobody has to read a justification to tell the two apart, and so the count is visible. Carrying
#: any of them means the same four steps the shipped axes took: emit it in
#: `for_feature_generation`, classify it in the right `enrich_llm._FEATURE_COLUMN_*` set, re-pin the
#: byte budget, then DELETE its entry here — which the staleness test below forces.
UNCARRIED_GAPS: dict[str, str] = {
    "business_term": (
        "GAP. The glossary's curated business NAME for the column ('Counterparty Exposure Amount' "
        "for `CPTY_EXPSR_AMT`). `for_concept_enrichment` and `for_summary` both send it as "
        "`term_name`; the feature seam sends neither it nor its tokens "
        "(`feature_assist._column_tokens` omits it too), so an objective phrased in the bank's own "
        "vocabulary cannot match the bank's own term."),
    "related_terms": (
        "GAP, and an ironic one. Task 6d added an LLM call to expand the OBJECTIVE with related "
        "business vocabulary, while the glossary's own curated related terms — already parsed, "
        "already persisted, already egress-classified as `_LIST_PROSE_META_KEYS`, and already sent "
        "by `for_concept_enrichment` — reach this seam not at all."),
    "fibo_path": (
        "DEFERRED by a recorded decision (progress ledger, 2026-08-07): not emitted on any "
        "feature seam today, and the reason is that it has no egress grade here yet, NOT that it "
        "is unwanted. `bian_path` and `process_path`, its two siblings from the same sidecar, ARE "
        "carried. Carrying it means one `_FEATURE_COLUMN_PROSE_KEYS` entry — the grade its "
        "unredacted origin (`glossary_reader.py`) requires, exactly as for `domain`."),
}


# ── the derived sides ───────────────────────────────────────────────────────────────────────────

def semantic_fields() -> frozenset[str]:
    """Side A: every field name the platform can resolve, project or carry — DERIVED, never listed.

    Six registries, in chokepoint order: a field must clear the first to be resolvable, the second
    to reach `graph_node`, and one of the last four to ride the bundle. A new enrichment field
    misses all six only if it is written nowhere, in which case there is nothing to leak."""
    return frozenset(
        set(_POLICIES)
        | set(_DISPLAY_COLUMN)
        | set(_DISPLAY_FIELDS)
        | set(_OPERATIONAL_FIELDS)
        | set(_GLOSSARY_SOURCE_FIELDS)
        | set(_TECHNICAL_SOURCE_FIELDS)
    )


def sentinel(field_name: str) -> str:
    """The one recognisable value this field carries into the bundle. Distinct per field, so
    finding it in the payload proves THIS field's value travelled — not that some key exists."""
    return f"carried::{field_name}"


_OBJECT_REF = "bank::public.payments.tran_amt"
_TABLE_REF = "bank::public.payments"

#: One current direct-equality link whose realization hops AWAY from the anchor's own table, so
#: `_outbound_cardinality` has a direction to answer for and the `relationships` entry is shaped the
#: way the egress adapter grades it.
_LINK = RelationshipContextV1(
    relationship_ref="bridge:cust",
    kind=RelationshipKind.DIRECT_EQUALITY.value,
    left_ref=_TABLE_REF,
    right_ref="bank::public.customer",
    availability="available",
    review_status="human_verified",
    assessment_revision_id="assess-1",
    realizations=(DirectionalRealizationContextV1(
        realization_revision_id="rr-1",
        from_ref=_TABLE_REF,
        to_ref="bank::public.customer",
        lifecycle="active",
        safety_status="deterministically_validated",
        cardinality=Cardinality.MANY_TO_ONE.value,
        scope_id="scope-1",
        sandbox_eligible=True,
        production_eligible=True,
    ),),
    producer="taxonomy",
    strength="confirmed",
    lifecycle="active",
    current=True,
    evidence_ids=("ev-1",),
)


def _semantic_value(field_name: str, *, unresolved: bool) -> SemanticValueV1:
    """One field's bundle entry carrying `sentinel(field_name)`.

    `unresolved=True` is the `_MEASURE_ANNOTATION` shape: the operational read model resolved
    NOTHING and the sentinel rides `proposed_value` instead — the state Task 6 taught the payload to
    carry as a `proposed_value` subkey rather than announce and drop."""
    evidence = (EvidenceAuthorityV1(producer="llm", strength="proposed", lifecycle="active"),)
    if unresolved:
        return SemanticValueV1(
            field_name=field_name, value=None, evidence=evidence,
            resolution_status=UNRESOLVED_PENDING_REVIEW, operational_influence="hint",
            proposed_value=sentinel(field_name))
    return SemanticValueV1(
        field_name=field_name, value=sentinel(field_name), evidence=evidence,
        resolution_status="current", operational_influence="hint")


def fully_populated_bundle(*, proposed_only: frozenset[str] = frozenset()) -> SemanticContextBundleV1:
    """A bundle carrying a distinct sentinel for EVERY side-A field, at every grain. No database.

    Each field is placed in `source_semantics`, `resolved_semantics` AND `table_context` with the
    same sentinel. That is deliberate: the fixture cannot know which grain a future field belongs
    to, and the question this test asks is "does the adapter read this field AT ALL", not "by which
    of its two readers". The limit is stated so nobody reads more into a pass — an adapter that read
    a column field through `_table_value` would still pass here, and would still be wrong.

    `proposed_only` names the fields whose `value` is None so their sentinel rides `proposed_value`.
    """
    values = tuple(
        _semantic_value(name, unresolved=name in proposed_only)
        for name in sorted(semantic_fields()))
    return SemanticContextBundleV1(
        contract_version=CONTRACT_VERSION,
        catalog_source="bank",
        object_ref=_OBJECT_REF,
        table_ref=_TABLE_REF,
        source_semantics=values,
        resolved_semantics=values,
        concept_path=("monetary_flow", "measure"),
        identifier_namespace=IdentifierNamespaceV1(
            scheme="cif", issuer_scope="bank", basis="catalog_scope"),
        table_context=values,
        catalog_profile_revision_id="cpr-1",
        dataset_profile_hash="dph-1",
        neighbouring_columns=(),
        relationship_context=(_LINK,),
        observation_context=(),
        missing_context=("definition_missing",),
    )


def _scalars(obj) -> set:
    """Every leaf value anywhere in the payload — nested wrappers, lists and dicts included. The
    sentinels the shipped facts carry live one level down, inside `{value, authority}`."""
    if isinstance(obj, dict):
        return {leaf for v in obj.values() for leaf in _scalars(v)}
    if isinstance(obj, (list, tuple)):
        return {leaf for v in obj for leaf in _scalars(v)}
    return {obj}


def _reaching_fields(payload: dict) -> set[str]:
    """The side-A fields whose sentinel is somewhere in `payload`."""
    leaves = _scalars(payload)
    return {name for name in semantic_fields() if sentinel(name) in leaves}


# ── the guard ───────────────────────────────────────────────────────────────────────────────────

def test_every_semantic_field_reaches_generation_or_is_explicitly_accounted_for():
    """The regression guard. A field the platform produces that neither reaches the feature payload
    nor appears in one of the two accounting maps is the exact defect this plan was written for."""
    reaching = _reaching_fields(for_feature_generation(fully_populated_bundle()))
    unaccounted = semantic_fields() - reaching - set(DELIBERATELY_OMITTED) - set(UNCARRIED_GAPS)
    assert not unaccounted, (
        f"{sorted(unaccounted)} are produced by the platform and never reach feature generation. "
        "Emit them in `semantic_context.for_feature_generation` and classify them in the matching "
        "`enrich_llm._FEATURE_COLUMN_*` set; or, if generation genuinely should not see them, add "
        "them to DELIBERATELY_OMITTED with the reason. If they SHOULD be carried and this change "
        "is not the place to do it, add them to UNCARRIED_GAPS — but that is a finding to report, "
        "not a way to make this test quiet.")


def test_the_accounting_names_only_fields_that_are_still_uncarried():
    """The other rot direction, and the one that has bitten this plan twice: an accounting entry
    that stopped describing reality.

    The brief for this task proposed excluding `sensitivity_display` — a field that is in none of
    the six registries, so the entry would have guarded nothing from the day it was written. And
    when somebody closes one of the UNCARRIED_GAPS, its entry must go, or the next reader is told a
    carried field is missing."""
    accounted = set(DELIBERATELY_OMITTED) | set(UNCARRIED_GAPS)
    assert not accounted - semantic_fields(), (
        f"{sorted(accounted - semantic_fields())} are accounted for but are not fields the "
        "platform produces — the entry guards nothing. Delete it.")
    assert not set(DELIBERATELY_OMITTED) & set(UNCARRIED_GAPS), (
        "a field is either a decision or a finding, never both")
    reaching = _reaching_fields(for_feature_generation(fully_populated_bundle()))
    assert not accounted & reaching, (
        f"{sorted(accounted & reaching)} DO reach feature generation now — delete their accounting "
        "entries, or the next reader is told a carried field is missing.")


def test_every_key_the_feature_payload_emits_is_egress_classified():
    """The neighbouring leak, one link further on: `sanitize_feature_context` fails CLOSED on an
    unclassified key — the terminal `else: return None` refuses the WHOLE payload, so one new key
    kills feature generation for every column at once, silently, with the flag on.

    Derived on both sides, and nested keys included: `proposed_value` and `outbound_cardinality`
    both landed one level down, which is where the shipped golden-payload test cannot see them (it
    drops empty values, so a key its fixture leaves unset is never graded)."""
    payload = for_feature_generation(fully_populated_bundle(proposed_only=frozenset({"unit"})))
    classified = (
        _FEATURE_COLUMN_IDENTITY_KEYS | _FEATURE_COLUMN_PROSE_KEYS | _FEATURE_COLUMN_FACT_KEYS
        | _FEATURE_COLUMN_TOKEN_LIST_KEYS | _FEATURE_COLUMN_MAPPING_KEYS
        | _FEATURE_COLUMN_DEFINITION_KEYS | set(_FEATURE_COLUMN_OBJECT_KEYS)
        | set(_FEATURE_COLUMN_DICT_LIST_KEYS))
    assert not set(payload) - classified, (
        f"{sorted(set(payload) - classified)} are emitted by `for_feature_generation` but graded by "
        "no `enrich_llm._FEATURE_COLUMN_*` set — every feature-generation call would be refused "
        "whole.")

    # The fact wrappers: `value` / `authority` / the optional `proposed_value`, and nothing else.
    assert "proposed_value" in payload["unit"], (
        "the fixture no longer exercises the proposed_value subkey — the nested grade below is "
        "asserting nothing")
    for key in _FEATURE_COLUMN_FACT_KEYS & set(payload):
        assert not set(payload[key]) - _FEATURE_FACT_SUBKEYS, (
            f"fact wrapper {key!r} carries subkeys outside `_FEATURE_FACT_SUBKEYS`")
    for key, allowed in _FEATURE_COLUMN_OBJECT_KEYS.items():
        if payload.get(key) is not None:
            assert not set(payload[key]) - allowed, f"object key {key!r} carries an ungraded subkey"
    for key, allowed in _FEATURE_COLUMN_DICT_LIST_KEYS.items():
        for entry in payload.get(key) or ():
            assert not set(entry) - allowed, f"{key} entry carries an ungraded subkey"
    assert payload["relationships"], (
        "the fixture no longer carries a relationship — the entry grade above is asserting nothing")
