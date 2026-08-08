"""One-off: propose an ``is_a`` parent for the registry concepts that declare none (plan Task 9b).

**This is not a per-upload stage.** It runs over ``concepts.py`` — a Python module, not a table —
emits a source patch, and is done. There is no migration, no DB connection and no catalog: the only
thing it reads is the platform-authored registry, so nothing customer-owned can reach a provider.

WHY A PARENT IS WORTH ADDING. ``concept_path`` (the selected concept followed by its ``is_a``
ancestors) rides in the feature-generation payload, the context graph and the asset-detail render.
A concept with no parent produces a one-element path, so a general question never reaches a specific
column through the hierarchy — ``category_code`` does not find ``mcc``, because nothing records that
one is a kind of the other.

WHY IT IS SAFE TO DO WITHOUT A HUMAN APPROVING EACH PAIR. Nothing operational gates on ``is_a``.
Join candidacy, bridge admission and Pass C all run on ``namespace`` — the three-axis model's only
join gate — and no module outside this one and ``attest/concept_critic`` reads the field at all. A
wrong parent degrades an ancestor list in a prompt; it cannot make two columns joinable, clear a
safety check, or change a governed verdict.

WHAT IS NOT SAFE, AND IS THEREFORE VALIDATED RATHER THAN TRUSTED. ``_validate_registry`` fails the
IMPORT on an ``is_a`` that does not resolve or that closes a cycle, and ``concept_path`` raises on a
cycle it walks. An unvalidated proposal does not degrade the vocabulary — it stops the application
booting. So every answer passes :func:`keep_valid` before it is rendered, a rejected proposal is a
normal outcome rather than an error, and the import guard remains the backstop that has to hold
even if this module has a hole.

TWO SOURCES, ONE VALIDATOR:

* :func:`propose_parents` — asks an ``LLMClient`` in bounded batches (the live path);
* :func:`offline_proposals` — replays :data:`OFFLINE_PROPOSALS`, the curated set that was actually
  applied, with no provider call at all.

Both funnel through :func:`keep_valid`, so the curated set is held to exactly the rules a model's
answer is held to. Once a patch has been applied, re-running either path proposes nothing for the
concepts it covered (they now have parents) — which is how you check a patch landed in full.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from featuregen.intake.llm import LLMClient, LLMRequest
from featuregen.overlay.upload.concepts import _ALL, _LEGACY_ALIASES, CONCEPT_REGISTRY, Concept

PROPOSER_TASK = "concept_parent_proposal"
PROPOSER_PROMPT_ID = "concept_parent_proposal"
PROPOSER_PROMPT_VERSION = 1
PROPOSER_SCHEMA_ID = "concept_parent_proposal"
PROPOSER_SCHEMA_VERSION = 1

#: Registry entries are small (a name, a group and one clause of prose), so a batch of 40 is well
#: inside a single response while keeping any one bad answer contained to its own chunk.
BATCH_SIZE = 40

#: Length of the description clause sent as the routing hint — the same first-sentence slice
#: `classification_vocabulary` and the concept critic already send, so the model reads the sentence
#: the registry authors wrote for exactly this purpose.
HINT_LEN = 150

PROPOSAL_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["assignments"],
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["concept", "parent"],
                "properties": {
                    "concept": {"type": "string"},
                    # "" means "no honest parent exists in this vocabulary" — a first-class answer,
                    # not a failure. Forcing a parent is how a false hierarchy gets authored.
                    "parent": {"type": "string"},
                },
            },
        },
    },
}

INSTRUCTION = (
    "You are extending a controlled banking concept vocabulary. For each concept below, name the "
    "ONE existing concept from candidate_parents that it is a KIND OF — a strict generalisation, "
    "so that 'every X is a Y' is literally true. Answer with an empty string when no candidate is a "
    "true generalisation; an empty answer is correct and expected for most concepts. Never answer "
    "with a sibling, a component, a co-occurring column, or the concept itself. Never invent a name."
)

#: Answers that mean "no parent" rather than naming one.
_NO_PARENT = frozenset({"", "none", "null", "n/a", "no parent"})


def parentless_records() -> tuple[Concept, ...]:
    """Every registry concept that declares no ``is_a`` and is not a superseded legacy alias.

    Legacy aliases are excluded at the SOURCE as well as in :func:`keep_valid`: they are retained
    only so already-enriched columns are never orphaned, they are already excluded from
    `classification_vocabulary`, and giving one a parent would add ancestry to a name the classifier
    is not allowed to choose.
    """
    return tuple(c for c in _ALL if c.is_a is None and c.name not in _LEGACY_ALIASES)


def candidate_parents() -> tuple[dict[str, str], ...]:
    """The names a proposal may draw from: every non-alias registry member, with its group and hint."""
    return tuple({"name": c.name, "group": c.group, "hint": _hint(c)}
                 for c in _ALL if c.name not in _LEGACY_ALIASES)


def _hint(record: Concept) -> str:
    return record.description.split(".")[0].strip()[:HINT_LEN]


def _parent_of(name: str, accepted: Mapping[str, str]) -> str | None:
    """The parent of ``name`` under the registry AS IT WOULD BE with ``accepted`` applied."""
    if name in accepted:
        return accepted[name]
    record = CONCEPT_REGISTRY.get(name)
    return record.is_a if record is not None else None


def _would_cycle(name: str, parent: str, accepted: Mapping[str, str]) -> bool:
    """Would ``name is_a parent`` close a loop, given the parents accepted so far in this run?

    Walks the chain ABOVE ``parent`` — through ``accepted`` first, then the shipped registry —
    because two proposals can each be acyclic on their own and jointly form a loop (A -> B accepted,
    then B -> A). Terminates unconditionally: ``seen`` grows by one name per step over a finite
    vocabulary, so even a registry that is ALREADY corrupt reports a cycle instead of spinning.
    """
    seen = {name}
    cur: str | None = parent
    while cur is not None:
        if cur in seen:
            return True
        seen.add(cur)
        cur = _parent_of(cur, accepted)
    return False


def keep_valid(proposals: Mapping[str, str],
               records: Sequence[Concept] | Iterable[Concept]) -> tuple[dict[str, str], dict[str, str]]:
    """Split ``{concept: parent}`` into ``(kept, dropped)``, where ``dropped`` maps name -> reason.

    The rules, in order, and why each one exists:

    * ``no_parent_offered`` — an empty/`none` answer. The expected outcome for most concepts.
    * ``legacy_alias`` — a superseded name neither gains ancestry nor becomes anyone's ancestor.
    * ``unknown_concept`` — the answer names something that is not in the vocabulary at all.
    * ``already_parented`` — the authored parents are identity; a proposal may ADD, never overwrite.
    * ``not_asked`` — the answer names a concept this batch did not ask about. A model volunteering
      an opinion about the rest of the vocabulary is not an answer to the question that was posed.
    * ``parent_is_legacy_alias`` / ``unknown_parent`` — an unresolved ``is_a`` fails the import.
    * ``self_parent`` — a one-step cycle.
    * ``cycle`` — including cycles formed jointly with parents accepted earlier in this run.

    ``already_parented`` deliberately outranks ``not_asked``: once a patch has been applied the
    covered concepts drop OUT of ``parentless_records()``, and re-running the proposer should report
    "there is already a parent here" rather than accusing the source of answering an unasked
    question. The two rules cover different faults and must not be confused in a run report.

    Deterministic: proposals are processed in sorted name order, so which member of a mutually
    cyclic pair survives does not depend on dict ordering.
    """
    asked = {c.name for c in records}
    kept: dict[str, str] = {}
    dropped: dict[str, str] = {}
    for name in sorted(proposals):
        parent = (proposals[name] or "").strip()
        if parent.lower() in _NO_PARENT:
            dropped[name] = "no_parent_offered"
        elif name in _LEGACY_ALIASES:
            dropped[name] = "legacy_alias"
        elif name not in CONCEPT_REGISTRY:
            dropped[name] = "unknown_concept"
        elif CONCEPT_REGISTRY[name].is_a is not None:
            dropped[name] = "already_parented"
        elif name not in asked:
            dropped[name] = "not_asked"
        elif parent in _LEGACY_ALIASES:
            dropped[name] = "parent_is_legacy_alias"
        elif parent not in CONCEPT_REGISTRY:
            dropped[name] = "unknown_parent"
        elif parent == name:
            dropped[name] = "self_parent"
        elif _would_cycle(name, parent, kept):
            dropped[name] = "cycle"
        else:
            kept[name] = parent
    return kept, dropped


def _dispatch(client: LLMClient, records: Sequence[Concept]) -> dict[str, str]:
    """Ask the model, in batches of :data:`BATCH_SIZE`, and merge the raw (unvalidated) answers.

    A malformed batch response is skipped rather than raised: this is a one-off over a static file,
    and losing one chunk of proposals costs a re-run, while aborting mid-way costs the whole run.
    A later batch never overwrites an earlier answer for the same name.
    """
    answers: dict[str, str] = {}
    parents = list(candidate_parents())
    for start in range(0, len(records), BATCH_SIZE):
        batch = list(records[start:start + BATCH_SIZE])
        request = LLMRequest(
            task=PROPOSER_TASK,
            prompt_id=PROPOSER_PROMPT_ID,
            prompt_version=PROPOSER_PROMPT_VERSION,
            inputs={"catalog_metadata": {
                "concepts": [{"name": c.name, "group": c.group, "hint": _hint(c)} for c in batch],
                "candidate_parents": parents,
            }},
            output_schema_id=PROPOSER_SCHEMA_ID,
            output_schema_version=PROPOSER_SCHEMA_VERSION,
            generation_settings={},
            output_schema=PROPOSAL_SCHEMA,
        )
        result = client.call(request)
        for row in (result.output or {}).get("assignments") or ():
            if not isinstance(row, Mapping):
                continue
            name, parent = row.get("concept"), row.get("parent")
            if isinstance(name, str) and isinstance(parent, str) and name not in answers:
                answers[name] = parent
    return answers


def propose_parents_with_reasons(
        client: LLMClient,
        records: Sequence[Concept]) -> tuple[dict[str, str], dict[str, str]]:
    """:func:`propose_parents` plus the per-name drop reasons, for the command's run report."""
    return keep_valid(_dispatch(client, list(records)), records)


def propose_parents(client: LLMClient, records: Sequence[Concept]) -> dict[str, str]:
    """``{concept: parent}``, keeping ONLY answers that survive every registry rule.

    Validation is not advisory here: ``_validate_registry`` fails the whole import on a bad ``is_a``,
    so an unvalidated proposal does not degrade the vocabulary — it stops the application booting.
    Drops are silent and normal; :func:`propose_parents_with_reasons` exposes the reasons.
    """
    kept, _ = propose_parents_with_reasons(client, records)
    return kept


def offline_proposals(records: Sequence[Concept] | Iterable[Concept]) -> dict[str, str]:
    """The deterministic, provider-free path: :data:`OFFLINE_PROPOSALS` through the SAME validator."""
    kept, _ = keep_valid(OFFLINE_PROPOSALS, records)
    return kept


def render_patch(kept: Mapping[str, str], dropped: Mapping[str, str] | None = None) -> str:
    """The source patch: one line per accepted pair naming the exact ``is_a=`` argument to add.

    It EMITS; it does not edit. ``concepts.py`` is source code and stays that way — this lands as a
    reviewed code change, not as a governance approval step.
    """
    lines = ["# is_a additions for src/featuregen/overlay/upload/concepts.py",
             f"# kept={len(kept)} dropped={len(dropped or {})}"]
    if dropped:
        tally: dict[str, int] = {}
        for reason in dropped.values():
            tally[reason] = tally.get(reason, 0) + 1
        lines += [f"#   dropped {reason}={count}" for reason, count in sorted(tally.items())]
    lines += [f'{name}\tis_a="{parent}"' for name, parent in sorted(kept.items())]
    return "\n".join(lines) + "\n"


# ── the curated offline proposal set ─────────────────────────────────────────────────────────────
#
# PROVENANCE, stated plainly: these pairs were authored offline against each concept's OWN
# declarations (its `group`, `pit_role`, `additivity`, `descriptive` flag and the first clause of its
# description) rather than generated by a provider call — the live run this plan schedules had not
# happened when the backfill landed. They are held to the same validator as a model's answer
# (`test_the_offline_proposals_all_survive_validation`) and they land as a reviewed source diff,
# which is exactly the shape the LLM path was designed to produce.
#
# THE RULE APPLIED: "every X is a Y" must be literally true, and Y must already be in the registry.
# Where no existing concept is a true generalisation, the concept STAYS A ROOT. That is why this set
# does not cover all 272 — see the module docstring of the report and, concretely:
#
#   * the 49 `identifier` concepts have no abstract root to point at, and cannot get one: a new
#     `party_identifier` would be `group="identifier"`, and `_validate_registry` requires every
#     identifier concept to declare exactly one `namespace` — an abstract identifier has none.
#     Parenting them to each other instead (`merchant_id is_a customer_id`) would assert a
#     falsehood, and would do it on the axis a reader is most likely to confuse with joinability.
#   * `limit` and `profit_rate` document their own root-ness ("NOT is_a monetary_stock",
#     "Deliberately NOT is_a monetary_rate") — the registry already answered, and the answer is no.
#   * `geolocation` says it is "distinct from geographic", so it is not parented there.
#   * ESG scope 1/2/3 are siblings with no emissions root; `financed_emissions` IS a Scope-3
#     category (GHG Protocol cat. 15), so that one pair is true and is included.
OFFLINE_PROPOSALS: dict[str, str] = {
    # ── every boolean indicator generalises to the registry's generic boolean ────────────────────
    # `boolean_flag` is "Generic boolean flag"; each of these describes itself as a flag, an
    # indicator, a marker, or "whether ...". Nothing behavioural is inherited (leakage_anchor,
    # near_label and sensitivity stay on each concept), so this adds a generalisation and no claim.
    "delinquency_flag": "boolean_flag",
    "default_flag": "boolean_flag",
    "fraud_flag": "boolean_flag",
    "restructured_flag": "boolean_flag",
    "sanctions_hit_flag": "boolean_flag",
    "pep_flag": "boolean_flag",
    "sicr_flag": "boolean_flag",
    "npe_flag": "boolean_flag",
    "watchlist_hit_flag": "boolean_flag",
    "adverse_media_flag": "boolean_flag",
    "model_output": "boolean_flag",
    "data_quality_flag": "boolean_flag",
    "bureau_provenance": "boolean_flag",
    "near_miss_flag": "boolean_flag",
    "taxable_flag": "boolean_flag",
    "thin_file_flag": "boolean_flag",
    "nested_correspondent_flag": "boolean_flag",
    "new_to_bank_flag": "boolean_flag",
    "staff_indicator": "boolean_flag",
    "restriction_status": "boolean_flag",
    "nominee_indicator": "boolean_flag",
    "record_deleted_flag": "boolean_flag",
    "statement_visibility_flag": "boolean_flag",
    "green_flag": "boolean_flag",
    "sharia_compliant_flag": "boolean_flag",
    "deforestation_flag": "boolean_flag",
    "vulnerability_flag": "boolean_flag",

    # ── coded categories generalise to `category_code` ("Generic coded category") ────────────────
    # Included only where the concept's OWN first clause calls it a classification, a code, a type,
    # a status, a tier, a phase or an ordinal bucket. Deliberately excluded: `unit_of_measure` (a
    # unit, not a category), `source_system` and `reference_data`/`alternative_data` (provenance /
    # data-class markers), `corridor` (a country PAIR), `peer_group` (a cohort),
    # `settlement_finality` (a property), `regulatory_report_line` (a coordinate — but see
    # `finrep_corep_line` below), and the ISO party-role fields, which are parties.
    "module_id": "category_code",
    "product_type": "category_code",
    "account_type": "category_code",
    "transaction_type": "category_code",
    "debit_credit_indicator": "category_code",
    "channel": "category_code",
    "country_code": "category_code",
    "industry_code": "category_code",
    "mcc": "category_code",
    "instrument_type": "category_code",
    "lifecycle_state": "category_code",
    "limit_type": "category_code",
    "collateral_type": "category_code",
    "lien_seniority": "category_code",
    "position_direction": "category_code",
    "exposure_class": "category_code",
    "model_tier": "category_code",
    "settlement_status": "category_code",
    "payment_rail": "category_code",
    "scheme": "category_code",
    "iso20022_purpose_code": "category_code",
    "segment": "category_code",
    "tranche": "category_code",
    "waterfall_position": "category_code",
    "vesting": "category_code",
    "decumulation": "category_code",
    "resolution_group": "category_code",
    "root_cause_code": "category_code",
    "swift_message_type": "category_code",
    "customer_relationship_status": "category_code",
    "source_system_status": "category_code",
    "legal_entity_type": "category_code",
    "residency_status": "category_code",
    "restriction_reason": "category_code",
    "fatca_crs_classification": "category_code",
    "nostro_vostro": "category_code",
    "aisp_pisp_flag": "category_code",
    "event_type": "category_code",              # "Digital event classification"
    "delinquency_bucket": "category_code",      # "Ordinal delinquency bucket"
    "impairment_stage": "category_code",        # "IFRS9 stage 1/2/3 (ordinal)"
    "retention_class": "category_code",         # "Retention policy class"
    "finrep_corep_line": "regulatory_report_line",

    # ── the label beside a code generalises to `code_label` ──────────────────────────────────────
    # Each of these sets `descriptive=True` and says "(the label beside X_id)" in its own
    # description; `code_label` is the concept for exactly that, and also sets `descriptive=True`.
    "branch_name": "code_label",
    "relationship_manager_name": "code_label",
    "merchant_name": "code_label",
    "account_name": "code_label",
    "instrument_name": "code_label",
    "counterparty_name": "code_label",

    # ── free prose generalises to `free_text` ────────────────────────────────────────────────────
    "payment_narrative": "free_text",
    "kyc_narrative": "free_text",
    "unstructured_doc": "free_text",

    # ── a payee name is a party name ─────────────────────────────────────────────────────────────
    "beneficiary_name": "party_name",

    # ── temporal, parented by the concept's OWN declared `pit_role` ──────────────────────────────
    # pit_role="event" => it IS the timestamp of an occurrence; pit_role="effective" => it IS an
    # effective date; pit_role="system_time" => it IS a knowledge-time date. `valid_time` names both
    # the as-of and effective axes in its own description, so those two roots hang under it.
    # NOT parented: `ex_date` and `reporting_period` (pit_role "as_of", but neither is a DECISION
    # reference date — `reporting_period` is not even a date), `maturity_date`, `tenor`,
    # `duration_tenure`, `vintage`, `settlement_cycle`, `business_day_convention`.
    "as_of_date": "valid_time",
    "effective_date": "valid_time",
    "value_date": "effective_date",
    "record_date": "effective_date",
    "origination_date": "event_timestamp",
    "trade_date": "event_timestamp",
    "settlement_date": "event_timestamp",
    "pay_date": "event_timestamp",
    "booking_date": "system_time",

    # ── currency: the reporting and native codes ARE currency codes ──────────────────────────────
    # `is_currency_denomination` reads group + additivity, never `is_a`, so `denomination_concepts()`
    # is byte-identical before and after.
    "base_currency": "currency_code",
    "local_currency": "currency_code",
    "cross_rate": "fx_conversion_rate",

    # ── crypto ───────────────────────────────────────────────────────────────────────────────────
    "stablecoin": "digital_asset",
    "cbdc": "digital_asset",

    # ── monetary / accounting, parented by the concept's OWN declared additivity ─────────────────
    # semi_additive carrying amounts are stocks; an additive recognised amount is a flow; a
    # non_additive fee percentage is a rate.
    "fair_value": "monetary_stock",             # "a valuation stock", semi_additive
    "amortised_cost": "monetary_stock",         # "a balance", semi_additive
    "accrual": "monetary_flow",                 # accrued over a period, additive
    "expense_ratio": "monetary_rate",           # a % cost, non_additive
    "merchant_discount_rate": "monetary_rate",  # the acquiring fee %, non_additive

    # ── risk measures ────────────────────────────────────────────────────────────────────────────
    "pd": "score_probability",                  # score_probability's own description lists "PD"
    "potential_future_exposure": "monetary_stock",   # matches expected_exposure's authored parent

    # ── esg ──────────────────────────────────────────────────────────────────────────────────────
    "financed_emissions": "scope_3_emissions",  # GHG Protocol Scope 3, category 15

    # ── the one identifier pair that is true: a parent CIF is a customer id ──────────────────────
    # Same namespace ("cif") and same entity_link ("customer") as `customer_id`; the difference is
    # the ROLE the reference plays, which is the third axis, not the identifier's value space.
    "parent_customer_id": "customer_id",
}
