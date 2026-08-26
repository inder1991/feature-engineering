"""BR-18 — the transaction foundation pack, tranche A: 24 atomic executable primitives.

These are NEW recipes (nothing legacy to replace): the reusable atoms every later composite
builds on, each with its own output contract and its own expectation ref — formula fragments
are shared only as CODE helpers here, never as shared identity. The canonical exemplar is
``posted_debit_amount``: the complete contract the plan spells out — account output grain over
the transaction event grain, the full operand set (transaction id, account, amount, direction,
status, booking time, correction link), all four governed policies, trailing window, additive
over account and time within one currency, empty window = zero — and it is the pack's ONE
honestly-FORMULA_AUTHORABLE recipe, because its reviewed Formula-v2 expectation exists (the
BR-6 gold exemplar, pinned by hash in ``recipe_formula_expectations_v2``). Every other tranche-A
recipe is FORMULA_BLOCKED until its own expectation is reviewed — readiness is granted by the
registry, one review at a time, never by assertion.

Structural guarantees the acceptance names: reversals and failed transactions cannot silently
inflate posted activity (the posted recipes' eligibility EXCLUDES them by name and by policy,
and the reversal/failure recipes count them separately); mixed currencies cannot be summed
without the governed conversion (monetary outputs carry the currency policy); account and
customer grains are never conflated (every output grain here is account/counterparty — customer
rollups are separate recipes with an allocation policy, per the plan).
"""
from __future__ import annotations

import dataclasses

from featuregen.formula.schema_v3 import SelectionKind, SemanticRowSelectionV1
from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    OperandSpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
)
from featuregen.overlay.upload.recipes._shared import (
    dim,
    entity,
    event_ts,
    event_window,
    measure,
    status,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

PAY_BEHAVIOUR = "payments.behaviour"
PAY_OPS = "payments.operations"
CROSS_BORDER = "payments.cross_border"
ENGAGEMENT = "customer.engagement"

TXN_ELIGIBLE = "eligible_status:foundation-posted-events"
TXN_REVERSALS = "reversal_correction:foundation-flag-or-code"
TXN_SIGN = "direction_sign:foundation-signed-by-indicator"
TXN_CCY = "currency_conversion:foundation-base-currency"

#: C-A3b: the posted-eligibility contract for a recipe that also SELECTS by direction. Declaring a
#: ``direction/…`` selection without a governed direction convention would be a recipe reading a
#: direction column with no rule for what its values mean — the validation refuses it.
_POSTED_DIRECTIONAL_ELIGIBILITY = EligibilitySpecV2(
    included="posted transactions in an eligible status, one direction",
    excluded="failed, reversed and technical events; the opposite direction",
    policy_refs=(TXN_ELIGIBLE, TXN_REVERSALS, TXN_SIGN))

_POSTED_ELIGIBILITY = EligibilitySpecV2(
    included="posted transactions in an eligible status",
    excluded="failed, reversed and technical events — a reversal or a decline never inflates "
             "posted activity; the failure/reversal recipes count those separately",
    policy_refs=(TXN_ELIGIBLE, TXN_REVERSALS))


def _base_operands(*, with_amount: bool = False, with_direction: bool = False,
                   event_time: OperandSpecV2 | None = None,
                   extra: tuple[OperandSpecV2, ...] = ()) -> tuple[OperandSpecV2, ...]:
    """The pack's shared slots. ``event_time`` REPLACES the generic event clock rather than adding
    a second one: a recipe whose window rides a specific timestamp (A6b's posting window) would
    otherwise carry a required generic ``event_timestamp`` slot that must be bound and is never
    read — a binding demand with no computation behind it."""
    ops = [entity("account", "account_id", "transaction"),
           event_time or event_ts("transaction")]
    if with_amount:
        ops.append(OperandSpecV2(
            role="amount", concept="monetary_flow", operand_class="measure",
            allowed_source_grains=("transaction",), unit_expectation="monetary",
            currency_expectation="per-row currency",
            sign_direction_expectation="unsigned amount plus direction authority, or signed "
                                       "values under the governed sign policy — a declared "
                                       "choice, never inferred"))
    if with_direction:
        ops.append(OperandSpecV2(
            role="direction", concept="debit_credit_indicator", operand_class="direction",
            allowed_source_grains=("transaction",), status_policy_ref=TXN_ELIGIBLE))
    ops.append(status("status", "booking_status", "transaction", policy=TXN_ELIGIBLE))
    ops.extend(extra)
    return tuple(ops)


def _count_output(output_id: str, label: str) -> OutputSpecV2:
    return OutputSpecV2(
        output_id=output_id, display_label=label,
        output_type="integer", additivity="additive", unit_kind="count",
        null_input_policy="ineligible rows are excluded, not nulled",
        empty_population_policy="an empty window returns zero")


def _amount_output(output_id: str, label: str) -> OutputSpecV2:
    return OutputSpecV2(
        output_id=output_id, display_label=label,
        output_type="numeric", additivity="additive", unit_kind="monetary",
        unit_policy="base currency units", currency_policy=TXN_CCY,
        null_input_policy="null amounts are rejected or ignored per the reviewed source policy",
        empty_population_policy="an empty window returns zero",
        aggregation_over_entity="sum across accounts within one currency",
        aggregation_over_time="sum over disjoint windows")


def _rate_output(output_id: str, label: str) -> OutputSpecV2:
    return OutputSpecV2(
        output_id=output_id, display_label=label,
        output_type="numeric", additivity="non_additive", unit_kind="ratio",
        valid_range="[0, 1]",
        null_input_policy="rows with unknown status are excluded from both sides",
        empty_population_policy="no eligible transactions returns null",
        zero_denominator_policy="zero transactions returns null")


def _DIRECTION_SELECTION(value: str) -> SemanticRowSelectionV1:
    """The structural statement a recipe NAME used to carry alone.

    ``posted_debit_amount`` and ``posted_credit_amount`` were otherwise identical — same operands,
    same policy refs — so "debit" existed only in the name and the prose, and a deterministic
    author could reach it only by inference.
    """
    return SemanticRowSelectionV1(
        kind=SelectionKind.TRANSACTION_DIRECTION, role="direction", semantic_value=value)


def _recipe(recipe_id: str, *, definition: str, context: str, output: OutputSpecV2,
            result_class: str, operands: tuple[OperandSpecV2, ...],
            objective: str = PAY_BEHAVIOUR,
            eligibility: EligibilitySpecV2 = _POSTED_ELIGIBILITY,
            readiness: str = "FORMULA_BLOCKED",
            revision: int = 1,
            row_selections: tuple[SemanticRowSelectionV1, ...] = (),
            event_time_role: str = "event_ts",
            expectation_ref: str | None = None) -> RecipeDefinitionV2:
    return RecipeDefinitionV2(
        recipe_id=recipe_id, revision=revision, family="transaction_foundation",
        primary_objective=objective,
        business_definition=definition, decision_context=context,
        computation_kind="deterministic_formula",
        output=output, operands=operands, row_selections=row_selections,
        source_grain="transaction", output_grain="account",
        temporal=event_window(event_time_role),
        readiness=readiness, parameters=(_WINDOW,),
        eligibility=eligibility,
        formula=FormulaReferenceV2(
            formula_schema_version="formula-v2",
            expectation_ref=expectation_ref or f"foundation:{recipe_id}",
            result_class=result_class))


_REVERSAL_ELIGIBILITY = EligibilitySpecV2(
    included="reversal/correction events linked to their originals",
    excluded="posted activity (the posted recipes' population — never double-counted here)",
    policy_refs=(TXN_REVERSALS,))

#: ── The G2 RULING for this pack's identifier-valued `dimension` slots ─────────────────────────
#: `transaction` (concept ``transaction_id``) and `original_txn` (concept
#: ``original_transaction_id``) are authored as ``dimension`` — the author's statement that the
#: slot carries a VALUE the recipe reads, not an aggregate. But both concepts are ENTITY-LINKED
#: (``transaction``), so ``need_metadata._derive_one`` derives ``intermediate_entity_key`` while
#: the class-keyed projection derives ``measure``. Two governed authorities, two answers, and
#: nobody had ruled: A6's serving gate names that ``OPERAND_ROLE_UNRESOLVED`` and refuses to
#: compute over the join until it is settled.
#:
#: **Settled here, and the ladder wins.** These slots hold the transaction's IDENTITY. Staged as
#: a MEASURE — which is what the platform silently does today — an identifier would be handed to
#: the additivity matrix as something to aggregate, which is meaningless for a key and is exactly
#: the mis-classification G2 names; ``recipe_operand_policy``'s own ``IDENTIFIER_NOT_A_MEASURE``
#: law says the same thing from the binder's side. So both DECLARE
#: ``join_role="intermediate_entity_key"``: a declaration is the platform's first rung
#: (``planner/requests._projected_roles``), it beats both derivations, and it is what a ruling
#: looks like. Nothing else about either operand changes, and no formula reads either slot — the
#: reviewed gold exemplar (``gold_v2/30_posted_debit_amount_exemplar.json``) reads only
#: direction / amount / booking time / account, and A6b's count recipe aggregates with COUNT_ROWS,
#: which consumes no operand at all. Both slots live as SHARED constants below, so the ruling is
#: made once and reaches every recipe that uses them.
_IDENTIFIER_KEY_ROLE = str(JoinRole.INTERMEDIATE_ENTITY_KEY)

#: The GOVERNED CANONICAL TRANSACTION IDENTITY — one declaration, shared by every recipe that
#: needs it, on the ``_CORRECTION_LINK`` precedent (one slot, one concept, one ruling: A6's
#: ruling on ``original_txn`` reached five recipes at once precisely because it was shared).
#:
#: **Ruling R13 lives on this slot.** ``COUNT_ROWS`` requires a governed transaction identity, and
#: a violated uniqueness produces ``TRANSACTION_IDENTITY_NOT_UNIQUE`` and NO count: preview renders
#: the guard, the fixture/run refuses. The operand exists so uniqueness can be **CHECKED** — it is
#: NOT a de-duplication argument, and nothing may quietly hand it to ``COUNT_DISTINCT``, which
#: would turn "refuse duplicates" into "count them once". Deduplication is chartered work (an
#: immutable ``TransactionDeduplicationPolicyRevision`` plus a typed survivor operator), not a
#: substitution. The COMPILED guard is the compiler/IR increment's ("transaction uniqueness guard
#: (R13)" in its pipeline); what a recipe owns is naming the identity the guard checks.
_TRANSACTION_IDENTITY = dataclasses.replace(
    dim("transaction", "transaction_id", "transaction"), join_role=_IDENTIFIER_KEY_ROLE)

#: The POSTING clock. ``event_timestamp`` is the generic "something happened then" concept; a
#: POSTED-activity recipe's window is a claim about when the entry was BOOKED, which is the
#: platform's ``booking_date`` concept (the journey's frozen column ``pstd_date`` carries exactly
#: it, and the reviewed gold exemplar's own window already reads ``booking_ts``).
_POSTING_TS = event_ts("transaction", role="booking_ts", concept="booking_date")

_CORRECTION_LINK = OperandSpecV2(
    role="original_txn", concept="original_transaction_id", operand_class="dimension",
    required=False, allowed_source_grains=("transaction",),
    join_role=_IDENTIFIER_KEY_ROLE)


TRANSACTION_FOUNDATION_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── the canonical exemplar: the complete contract, honestly AUTHORABLE ──────────────────────
    _recipe("posted_debit_amount",
            definition=("Sum of eligible posted DEBIT economic amount per account over the "
                        "trailing window — direction from the governed authority, reversals "
                        "neutralized per policy, per-row currencies converted only through the "
                        "governed rate at booking time; additive over account and time within "
                        "one currency; an empty window returns zero."),
            context="the canonical spend primitive (the BR-18 exemplar)",
            output=_amount_output("posted_debit_amount", "Posted debit amount"),
            result_class="sum",
            operands=_base_operands(with_amount=True, with_direction=True, extra=(
                # the G2 ruling above: an identifier-valued slot is a KEY, never a measure
                _TRANSACTION_IDENTITY,
                _CORRECTION_LINK,
                event_ts("transaction", role="booking_ts", concept="booking_date",
                         group="txn_times"),
                event_ts("transaction", role="value_ts", concept="value_date",
                         group="txn_times"))),
            eligibility=EligibilitySpecV2(
                included="posted debits in an eligible status, corrections linked",
                excluded="failed, reversed and technical events; sums across unconverted "
                         "currencies",
                policy_refs=(TXN_ELIGIBLE, TXN_REVERSALS, TXN_SIGN, TXN_CCY)),
            readiness="FORMULA_AUTHORABLE",
            row_selections=(_DIRECTION_SELECTION("debit"),),
            expectation_ref="posted_debit_amount"),
    _recipe("posted_credit_amount",
            definition="Sum of eligible posted CREDIT amount per account over the window.",
            context="the income-side primitive",
            output=_amount_output("posted_credit_amount", "Posted credit amount"),
            result_class="sum",
            operands=_base_operands(with_amount=True, with_direction=True),
            eligibility=_POSTED_DIRECTIONAL_ELIGIBILITY,
            row_selections=(_DIRECTION_SELECTION("credit"),)),
    # ── A6b: the count recipe, made structurally honest under ruling R13 ────────────────────────
    # Revision 2. It said "debit" in its NAME and its prose while declaring no row selection; it
    # named no transaction identity, so "count of transactions" had nothing to be a count OF; and
    # its window rode the generic event clock while the thing counted is a POSTED transaction.
    # All three are now DECLARED — which is a semantic revision of the feature, hence the bump
    # (contrast A6's ruling on `posted_debit_amount`, which declared a `join_role` and did NOT
    # bump: a declaration settles what an existing slot already meant; a row selection, a new
    # required operand and a re-bound window change the feature itself).
    _recipe("posted_debit_transaction_count",
            definition=("Count of eligible posted DEBIT transactions per account over the "
                        "trailing POSTING window — direction from the governed authority, "
                        "reversals and failures excluded by policy, each row carrying the "
                        "governed transaction identity the uniqueness guard checks (a violated "
                        "identity refuses the run and produces no values; duplicates are never "
                        "silently de-duplicated); an empty window returns zero."),
            context="spend activity volume",
            output=_count_output("posted_debit_transaction_count", "Posted debit count"),
            result_class="count",
            revision=2,
            operands=_base_operands(with_direction=True, event_time=_POSTING_TS,
                                    extra=(_TRANSACTION_IDENTITY,)),
            eligibility=_POSTED_DIRECTIONAL_ELIGIBILITY,
            row_selections=(_DIRECTION_SELECTION("debit"),),
            # ONE source for the role name: the window names the operand it binds, so the two
            # cannot drift apart into `event_time_role_unbound`.
            event_time_role=_POSTING_TS.role),
    _recipe("posted_credit_transaction_count",
            definition="Count of eligible posted credit transactions over the window.",
            context="income activity volume",
            output=_count_output("posted_credit_transaction_count", "Posted credit count"),
            result_class="count",
            operands=_base_operands(with_direction=True)),
    _recipe("net_posted_transaction_flow",
            definition=("Signed sum (credits minus debits) of eligible posted amounts per "
                        "account, under the governed sign policy."),
            context="the net-flow primitive (account grain)",
            output=_amount_output("net_posted_transaction_flow", "Net posted flow"),
            result_class="sum",
            operands=_base_operands(with_amount=True, with_direction=True),
            eligibility=EligibilitySpecV2(
                included="posted transactions with a governed direction reading",
                excluded="failed, reversed and technical events",
                policy_refs=(TXN_ELIGIBLE, TXN_REVERSALS, TXN_SIGN, TXN_CCY))),
    _recipe("posted_transaction_average_amount",
            definition="Posted amount sum divided by posted count over the window.",
            context="ticket-size primitive",
            output=OutputSpecV2(
                output_id="posted_transaction_average_amount", display_label="Average amount",
                output_type="numeric", additivity="non_additive", unit_kind="monetary",
                unit_policy="base currency units", currency_policy=TXN_CCY,
                null_input_policy="null amounts are excluded per the reviewed source policy",
                empty_population_policy="no eligible transactions returns null",
                zero_denominator_policy="zero transactions returns null"),
            result_class="ratio",
            operands=_base_operands(with_amount=True)),
    _recipe("distinct_transaction_counterparty_count",
            definition="Distinct counterparties in eligible posted activity over the window.",
            context="relationship breadth primitive",
            output=_count_output("distinct_transaction_counterparty_count",
                                 "Distinct counterparties"),
            result_class="distinct_count",
            operands=_base_operands(extra=(
                dim("counterparty", "customer_id", "transaction"),))),
    _recipe("distinct_merchant_count",
            definition="Distinct merchants in eligible posted card activity over the window.",
            context="merchant breadth primitive",
            output=_count_output("distinct_merchant_count", "Distinct merchants"),
            result_class="distinct_count",
            operands=_base_operands(extra=(dim("merchant", "merchant_id", "transaction"),))),
    _recipe("active_transaction_day_count",
            definition="Days in the window with at least one eligible posted transaction.",
            context="activity-day primitive",
            objective=ENGAGEMENT,
            output=_count_output("active_transaction_day_count", "Active days"),
            result_class="count",
            operands=_base_operands()),
    _recipe("transaction_recency_days",
            definition="Days since the last eligible posted transaction at the cutoff.",
            context="recency primitive",
            objective=ENGAGEMENT,
            output=OutputSpecV2(
                output_id="transaction_recency_days", display_label="Transaction recency",
                output_type="numeric", additivity="non_additive", unit_kind="duration_days",
                null_input_policy="rows with null event time are excluded per policy",
                empty_population_policy="no eligible activity in the window returns null"),
            result_class="recency",
            operands=_base_operands()),

    # ── the failure/correction family: counted SEPARATELY, never inflating posted ───────────────
    _recipe("failed_transaction_count",
            definition="Count of FAILED transaction attempts over the window.",
            context="failure volume (its own population)",
            objective=PAY_OPS,
            output=_count_output("failed_transaction_count", "Failed count"),
            result_class="count",
            operands=_base_operands(),
            eligibility=EligibilitySpecV2(
                included="failed attempts",
                excluded="posted activity (a failure is not activity)",
                policy_refs=(TXN_ELIGIBLE,))),
    _recipe("failed_transaction_rate",
            definition="Failed attempts divided by all attempts over the window.",
            context="failure health",
            objective=PAY_OPS,
            output=_rate_output("failed_transaction_rate", "Failure rate"),
            result_class="ratio",
            operands=_base_operands(),
            eligibility=EligibilitySpecV2(
                included="all attempts with a recorded status",
                excluded="rows with unknown status",
                policy_refs=(TXN_ELIGIBLE,))),
    _recipe("reversal_count",
            definition="Count of reversal/correction events over the window.",
            context="correction volume",
            objective=PAY_OPS,
            output=_count_output("reversal_count", "Reversal count"),
            result_class="count",
            operands=_base_operands(extra=(
                status("reversal", "reversal_indicator", "transaction",
                       policy=TXN_REVERSALS),
                _CORRECTION_LINK)),
            eligibility=_REVERSAL_ELIGIBILITY),
    _recipe("reversal_amount",
            definition="Reversed amount over the window, in base currency.",
            context="correction value",
            objective=PAY_OPS,
            output=_amount_output("reversal_amount", "Reversal amount"),
            result_class="sum",
            operands=_base_operands(with_amount=True, extra=(
                status("reversal", "reversal_indicator", "transaction",
                       policy=TXN_REVERSALS),
                _CORRECTION_LINK)),
            eligibility=_REVERSAL_ELIGIBILITY),
    _recipe("reversal_rate",
            definition="Reversal events divided by posted transactions over the window.",
            context="correction health",
            objective=PAY_OPS,
            output=_rate_output("reversal_rate", "Reversal rate"),
            result_class="ratio",
            operands=_base_operands(extra=(
                status("reversal", "reversal_indicator", "transaction",
                       policy=TXN_REVERSALS),)),
            eligibility=EligibilitySpecV2(
                included="posted transactions and their reversal events",
                excluded="rows with unknown status",
                policy_refs=(TXN_ELIGIBLE, TXN_REVERSALS))),
    _recipe("refund_count",
            definition="Count of merchant refunds (credits reversing prior purchases).",
            context="refund volume",
            objective=PAY_OPS,
            output=_count_output("refund_count", "Refund count"),
            result_class="count",
            operands=_base_operands(with_direction=True, extra=(
                dim("txn_type", "transaction_type", "transaction"), _CORRECTION_LINK))),
    _recipe("refund_amount",
            definition="Refunded amount over the window, in base currency.",
            context="refund value",
            objective=PAY_OPS,
            output=_amount_output("refund_amount", "Refund amount"),
            result_class="sum",
            operands=_base_operands(with_amount=True, with_direction=True, extra=(
                dim("txn_type", "transaction_type", "transaction"), _CORRECTION_LINK))),
    _recipe("return_count",
            definition="Count of scheme returns (R-transactions) over the window.",
            context="return volume",
            objective=PAY_OPS,
            output=_count_output("return_count", "Return count"),
            result_class="count",
            operands=_base_operands(extra=(
                status("return_status", "payment_return_status", "transaction"),
                dim("reason", "return_reason_code", "transaction", required=False)))),
    _recipe("return_amount",
            definition="Returned amount over the window, in base currency.",
            context="return value",
            objective=PAY_OPS,
            output=_amount_output("return_amount", "Return amount"),
            result_class="sum",
            operands=_base_operands(with_amount=True, extra=(
                status("return_status", "payment_return_status", "transaction"),))),

    # ── corridors, cash, fees ───────────────────────────────────────────────────────────────────
    _recipe("cross_border_transaction_count",
            definition="Count of eligible posted cross-border transactions over the window.",
            context="cross-border volume",
            objective=CROSS_BORDER,
            output=_count_output("cross_border_transaction_count", "Cross-border count"),
            result_class="count",
            operands=_base_operands(extra=(dim("corridor", "corridor", "transaction"),))),
    _recipe("cross_border_amount",
            definition="Cross-border posted amount over the window, in base currency.",
            context="cross-border value",
            objective=CROSS_BORDER,
            output=_amount_output("cross_border_amount", "Cross-border amount"),
            result_class="sum",
            operands=_base_operands(with_amount=True, extra=(
                dim("corridor", "corridor", "transaction"),))),
    _recipe("cash_withdrawal_count",
            definition="Count of cash withdrawals (channel/instrument-identified).",
            context="cash usage volume",
            output=_count_output("cash_withdrawal_count", "Cash withdrawal count"),
            result_class="count",
            operands=_base_operands(extra=(
                dim("channel", "channel", "transaction"),
                dim("instrument", "instrument_type", "transaction")))),
    _recipe("cash_withdrawal_amount",
            definition="Cash withdrawn over the window, in base currency.",
            context="cash usage value",
            output=_amount_output("cash_withdrawal_amount", "Cash withdrawal amount"),
            result_class="sum",
            operands=_base_operands(with_amount=True, extra=(
                dim("channel", "channel", "transaction"),
                dim("instrument", "instrument_type", "transaction")))),
    _recipe("fee_amount",
            definition="Fees charged to the account over the window, in base currency.",
            context="fee primitive",
            output=_amount_output("fee_amount", "Fee amount"),
            result_class="sum",
            operands=_base_operands(with_amount=True, extra=(
                measure("fee", "monetary_flow", "transaction",
                        economic_role="fee_charged"),))),
)
