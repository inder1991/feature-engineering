"""A6 — the ACCOUNT-GRAIN journey fixtures: two catalogs and one AI-proposed identifier link.

Journeys D1-D4 (plan execution-order item 11) need a cross-catalog option that is genuinely
served: a transaction-grain event table in one catalog, an account-grain dimension in another,
and an AI-PROPOSED (never human-confirmed) link between them. Everything here is *setup* under
the plan's seeding rule — "source data, governed catalog fixtures, and physical sources MAY be
installed as setup; from hypothesis submission onward, every selection, realization, adoption,
formula, build set, and artifact is created through public APIs or the same production services
those APIs call". So this module seeds catalog rows through ``build_graph`` (the upload path's
own projection) and the link through the SAME overlay events ``bridge_propose`` appends, and it
stops there: no realization, no adoption, no selection, no formula.

**Why ACCOUNT grain.** §V9's grain law: ``posted_debit_amount`` anchors
``entity("account", "account_id", "transaction")``, so the recipe computes PER ACCOUNT. The
decisive customer-grain CIB/FTR journey needs Formula V4's joined-attribute predicate (plan step
6) and a customer-grain feature; it is deliberately NOT built on these fixtures. What keeps that
honest is :func:`account_grain_request`'s own output grain — a customer unit-of-analysis over
this recipe refuses ``UOA_MISMATCH`` at the lens's B10 rule, and A6 pins that refusal.

**The hypothesis is a LEVEL, never a spike.** ``posted_debit_amount`` is a windowed SUM: it
measures how much flowed out, not how much the outflow MOVED. "Accounts with high posted debit
outflows" is therefore the only wording these fixtures may carry;
``test_the_account_grain_hypothesis_is_a_level_never_a_spike`` pins it.

**Extension path (for the later customer-grain journey).** Nothing here is a single monolith:

* :func:`seed_transaction_catalog` / :func:`seed_account_catalog` each take ``extra`` rows, so a
  later journey ADDS a ``cif_id`` column (or a whole customer table via
  :func:`seed_catalog`) rather than editing this module's tables;
* :func:`propose_identifier_link` is parameterised over both endpoints, so a second
  AI-proposed link (customer ↔ transaction) is one more call, not a rewrite;
* :func:`account_grain_request` takes ``**overrides``, so a customer-grain variant re-uses the
  same donor recipe identity and changes only ``output_grain`` + the operand set.
"""
from __future__ import annotations

from typing import Any

from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact

from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.bridge_assessment import (
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    IdentifierLinkAssessmentV1,
    NamespaceVerdict,
    PopulationRelation,
    TypeBasis,
)
from featuregen.overlay.upload.bridge_store import record_candidate_assessment
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    RequiredOperandV1,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.contracts import CatalogScopeV1

#: The two catalogs. ``ops`` holds the transaction event log; ``core`` holds the account master.
TRANSACTION_CATALOG = "ops"
ACCOUNT_CATALOG = "core"
TRANSACTION_TABLE = "transactions"
ACCOUNT_TABLE = "accounts"

#: The AI-proposed identifier link the journeys are ABOUT: the transaction log's account number
#: against the account master's own key. Two different column names for one identifier is exactly
#: the shape a human never wrote down and the proposer found.
TRANSACTION_LINK_COLUMN = "account_number"
ACCOUNT_LINK_COLUMN = "acct_id"
ACCOUNT_BRIDGE_FACT_KEY = "bfk_a6_account_number_acct_id"

#: The submitted hypothesis, in the wording the recipe can honestly answer (a LEVEL — see the
#: module docstring). Journeys start from THIS string, never from a seeded selection.
ACCOUNT_GRAIN_HYPOTHESIS = "Find accounts with high posted debit outflows in the last 30 days."

#: The registry recipe whose identity the fixture request borrows (§V9's grain law).
DONOR_RECIPE_ID = "posted_debit_amount"

#: The operands the fixture request DROPS from the donor recipe, and why. Both are `dimension`
#: operands on an ENTITY-LINKED concept (`transaction_id` / `original_transaction_id` both link
#: the `transaction` entity), so they are two of the G2 worklist's 82 divergences: the recipe
#: author declared a value, the concept registry declares a key. A6's serving gate flags exactly
#: that, and A6 may not settle it (G2's ruling is chartered) — so the journey fixture computes the
#: same account-grain sum WITHOUT them rather than declaring a role nobody ruled on.
#: `test_the_donor_recipe_itself_carries_the_g2_divergence` pins that these are the two.
G2_DIVERGENT_DONOR_ROLES = ("transaction", "original_txn")


def seed_catalog(db, source: str, rows: tuple[tuple[CanonicalRow, str], ...]) -> None:
    """Seed one catalog source: ``(CanonicalRow, concept)`` pairs through the upload path's own
    graph projection, AND record the governed ``concept`` field evidence each column's meaning
    stands on.

    Both halves are needed and they are not the same fact. ``build_graph`` gives the planner a
    column with a concept, which is what binds a role; ``field_evidence`` is what
    ``field_resolution.current_resolution_pins`` reads, and A5's
    ``semantic_revisions_for_plan`` takes the winning row's ``evidence_id`` as R9's governed
    semantic revision. A fixture that seeds only the graph produces a plan that RESOLVES and then
    refuses ``GOVERNED_SEMANTIC_REVISION_MISSING`` — exactly the empty read A5's report flagged
    as its concern 5, and the reason these fixtures seed the evidence too.

    The evidence is HUMAN-CONFIRMED because the catalog is governed setup; the only AI-proposed
    thing in this world is the LINK (:func:`propose_identifier_link`)."""
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.column_authority import logical_ref_of

    build_graph(db, source, [row for row, _ in rows],
                concepts={content_hash(row): concept for row, concept in rows})
    for row, concept in rows:
        logical = logical_ref_of(db, source, f"public.{row.table}.{row.column}")
        record_field_evidence(
            db, logical_ref=logical, field_name="concept", proposed_value=concept,
            producer="human", strength="confirmed", producer_ref="user:catalog-owner",
            source_snapshot_id=f"snap-a6-{source}",
            input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                        material=concept))


def transaction_rows(
    extra: tuple[tuple[CanonicalRow, str], ...] = (),
) -> tuple[tuple[CanonicalRow, str], ...]:
    """The transaction event log's governed columns. ``extra`` is the extension seam — a later
    journey adds ``cif_id`` here instead of editing this tuple."""
    def row(column: str, type_: str, **kw: Any) -> CanonicalRow:
        return CanonicalRow(TRANSACTION_CATALOG, TRANSACTION_TABLE, column, type_, **kw)

    return (
        (row("transaction_id", "text", is_grain=True), "transaction_id"),
        (row(TRANSACTION_LINK_COLUMN, "text"), "account_id"),
        (row("amount", "numeric", additivity="additive", currency="USD"), "monetary_flow"),
        (row("direction", "text"), "debit_credit_indicator"),
        (row("status", "text"), "booking_status"),
        (row("event_ts", "timestamp"), "event_timestamp"),
        *extra,
    )


def account_rows(
    extra: tuple[tuple[CanonicalRow, str], ...] = (),
) -> tuple[tuple[CanonicalRow, str], ...]:
    """The account master's governed columns — the grain the feature lands on."""
    def row(column: str, type_: str, **kw: Any) -> CanonicalRow:
        return CanonicalRow(ACCOUNT_CATALOG, ACCOUNT_TABLE, column, type_, **kw)

    return (
        (row(ACCOUNT_LINK_COLUMN, "text", is_grain=True), "account_id"),
        *extra,
    )


def seed_transaction_catalog(db, extra: tuple[tuple[CanonicalRow, str], ...] = ()) -> None:
    seed_catalog(db, TRANSACTION_CATALOG, transaction_rows(extra))


def seed_account_catalog(db, extra: tuple[tuple[CanonicalRow, str], ...] = ()) -> None:
    seed_catalog(db, ACCOUNT_CATALOG, account_rows(extra))


def _endpoint(source: str, table: str, column: str, entity: str) -> IdentifierEndpointV1:
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=(IdentifierColumnMemberV1(
            normalize_ref(source, "public", table, column), "text", TypeBasis.DECLARED),),
        entity_id=entity)


def propose_identifier_link(
    db, fact_key: str, *, entity: str,
    left_source: str, left_table: str, left_column: str,
    right_source: str, right_table: str, right_column: str,
) -> str:
    """An AI-PROPOSED identifier link — DRAFT lifecycle, never confirmed by a human.

    DRAFT is inside ``cross_catalog_links.AVAILABLE_STATUSES``, so the link is usable before
    confirmation (that is the platform's ruling, not a fixture shortcut); the candidate
    ASSESSMENT is what carries the ordered endpoint members ``active_bridges`` projects. The
    events are the ones ``bridge_propose`` itself appends — nothing is written that production
    could not write."""
    govern_bridge_fact(
        db, fact_key, entity=entity,
        left_source=left_source, left_ref=f"public.{left_table}.{left_column}",
        right_source=right_source, right_ref=f"public.{right_table}.{right_column}",
        status="DRAFT")
    record_candidate_assessment(db, IdentifierLinkAssessmentV1(
        left_endpoint=_endpoint(left_source, left_table, left_column, entity),
        right_endpoint=_endpoint(right_source, right_table, right_column, entity),
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1",
        bridge_fact_key=fact_key), expected_pointer_version=0)
    return fact_key


def propose_account_link(db, fact_key: str = ACCOUNT_BRIDGE_FACT_KEY) -> str:
    """The journey's link: ``ops::…transactions.account_number ↔ core::…accounts.acct_id``."""
    return propose_identifier_link(
        db, fact_key, entity="account",
        left_source=TRANSACTION_CATALOG, left_table=TRANSACTION_TABLE,
        left_column=TRANSACTION_LINK_COLUMN,
        right_source=ACCOUNT_CATALOG, right_table=ACCOUNT_TABLE,
        right_column=ACCOUNT_LINK_COLUMN)


def seed_account_grain_world(db) -> str:
    """Both catalogs plus the AI-proposed link — the whole D1-D4 setup, in one call."""
    seed_transaction_catalog(db)
    seed_account_catalog(db)
    return propose_account_link(db)


def account_grain_scope(scope_id: str = "s_a6_account_grain") -> CatalogScopeV1:
    return CatalogScopeV1(
        scope_id=scope_id,
        authorized_catalog_sources=(TRANSACTION_CATALOG, ACCOUNT_CATALOG),
        catalog_state_stamps=(), omitted_catalog_sources=(),
        read_scope_policy_version="1.0.0", role_resolution_version="unknown",
        resolved_at="2026-08-24T00:00:00Z", catalog_consideration_truncated=False)


def account_grain_operands() -> tuple[RequiredOperandV1, ...]:
    """The donor recipe's BASE operand set — account anchor, event time, amount, direction,
    status — with the two roles the journey drops (see :data:`G2_DIVERGENT_DONOR_ROLES`).

    ``account`` and ``amount`` DECLARE their join role, which is the platform's own first rung
    (``requests._projected_roles``: a non-empty declaration wins outright). ``direction`` and
    ``status`` deliberately declare NOTHING — the two governed authorities already agree on them,
    and leaving them bare is what proves A6's gate does not fire on an agreeing operand."""
    return (
        RequiredOperandV1(role="account", concept="account_id", operand_class="entity_key",
                          allowed_source_grains=("transaction",),
                          join_role=str(JoinRole.SOURCE_ENTITY_KEY)),
        RequiredOperandV1(role="event_ts", concept="event_timestamp",
                          operand_class="event_timestamp",
                          allowed_source_grains=("transaction",)),
        RequiredOperandV1(role="amount", concept="monetary_flow", operand_class="measure",
                          allowed_source_grains=("transaction",),
                          join_role=str(JoinRole.MEASURE)),
        RequiredOperandV1(role="direction", concept="debit_credit_indicator",
                          operand_class="direction", allowed_source_grains=("transaction",)),
        RequiredOperandV1(role="status", concept="booking_status", operand_class="status",
                          allowed_source_grains=("transaction",)),
    )


def account_grain_request(**overrides: Any) -> FeaturePlanningRequestV1:
    """The journey's planning request: ``posted_debit_amount``'s identity at ACCOUNT grain.

    Output spec, temporal spec, objective, eligibility and formula reference are taken VERBATIM
    from the shipped V2 recipe, so this is a shape the platform genuinely produces rather than
    one invented for a test. ``**overrides`` is the extension seam."""
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    donor = v2_recipe_by_id(DONOR_RECIPE_ID)
    assert donor is not None, DONOR_RECIPE_ID
    values: dict[str, Any] = {
        "origin": "llm_intent",
        "source_definition_id": "a6_account_grain_posted_debit_outflow",
        "source_revision": "1",
        "source_content_hash": "a6accountgraincontenthash",
        "primary_objective": donor.primary_objective,
        "output": donor.output,
        "operands": account_grain_operands(),
        "source_grain": "transaction",
        "output_grain": "account",
        "temporal": donor.temporal,
        "computation_kind": donor.computation_kind,
        "eligibility": donor.eligibility,
        "formula": donor.formula,
        "parameter_values": (("window", 30),),
    }
    values.update(overrides)
    return FeaturePlanningRequestV1(**values)


def donor_recipe_request() -> FeaturePlanningRequestV1:
    """``posted_debit_amount`` projected VERBATIM — the registry's own request, G2 divergences
    and all. This is what A6's gate test uses: the divergence it catches is a real shipped
    recipe's, never a shape invented to make the gate fire."""
    from featuregen.overlay.upload.feature_planning_contracts import planning_request_from_recipe
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    donor = v2_recipe_by_id(DONOR_RECIPE_ID)
    assert donor is not None, DONOR_RECIPE_ID
    return planning_request_from_recipe(donor)
