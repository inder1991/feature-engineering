"""BR-5 — the V2 binding policy: ambiguity fails closed THROUGH the live tie-break seam, and
concept-compatibility never stands in for banking meaning.

The composition the amended invariant demands, proven end to end: an unadjudicated required tie is
AMBIGUOUS with no selected column (the exact opposite of the legacy surface's deterministic
fallback — which stays untouched, its own flag governing it); an ADJUDICATED tie binds through the
SAME verdict store the live surface reads, carrying the deliberation's reference. The semantic
half: an operand demanding an economic role binds only over human-confirmed evidence (a deposit
balance cannot satisfy a drawn-credit-exposure role because NOTHING satisfies it without
evidence); opposing legs refuse one physical column without a sign authority; and the formula
authority envelope rejects the same class with the same code — one verdict source for both paths.
"""
from __future__ import annotations

from dataclasses import replace

from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import logical_ref_of
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_operand_policy import (
    AMBIGUOUS_BY_CLASS,
    DISTINCT_BINDING_VIOLATED,
    ECONOMIC_ROLE_UNPROVEN,
    REQUIRED_OPERAND_MISSING,
    bind_v2_operands,
    governed_economic_role,
)
from featuregen.overlay.upload.recipe_registry_v2 import PROBE_RECIPE
from featuregen.overlay.upload.tie_break import (
    TieBreakCandidate,
    store_tie_break_verdict,
    tie_break_input_hash,
)

SOURCE = "v2bank"


def _catalog(db, *, second_amount: bool = False):
    rows = [
        (CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                      entity="Account", definition="the posting account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD", definition="signed transaction amount"), "monetary_flow"),
        (CanonicalRow(SOURCE, "transactions", "dc_flag", "text",
                      definition="debit/credit indicator"), "debit_credit_indicator"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp",
                      definition="when the transaction was booked"), "event_timestamp"),
    ]
    if second_amount:
        rows.append((CanonicalRow(SOURCE, "transactions", "orig_amount", "numeric",
                                  additivity="additive", currency="USD",
                                  definition="original pre-conversion amount"), "monetary_flow"))
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _confirm_economic_role(db, object_ref: str, value: str) -> None:
    logical = logical_ref_of(db, SOURCE, object_ref)
    record_field_evidence(
        db, logical_ref=logical, field_name="economic_role", proposed_value=value,
        producer="human", strength="confirmed", producer_ref="user:sme",
        source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="economic_role",
                                    material=value))


def test_a_clean_catalog_binds_every_operand_uniquely(db):
    _catalog(db)
    verdicts = bind_v2_operands(db, PROBE_RECIPE, catalog_source=SOURCE, roles=("data_owner",))
    assert [v.status for v in verdicts] == ["bound"] * 4
    by_role = {v.role: v for v in verdicts}
    assert by_role["amount"].selected_ref == "public.transactions.amount"
    assert by_role["amount"].tie_break_verdict_ref is None


def test_an_unadjudicated_required_tie_fails_closed_with_the_resolution_named(db):
    _catalog(db, second_amount=True)
    verdicts = bind_v2_operands(db, PROBE_RECIPE, catalog_source=SOURCE, roles=("data_owner",))
    amount = next(v for v in verdicts if v.role == "amount")
    assert amount.status == "ambiguous"
    assert amount.selected_ref is None, "no selected column — the executable path never guesses"
    assert set(amount.tied_refs) == {"public.transactions.amount",
                                     "public.transactions.orig_amount"}
    assert amount.reason_codes == (AMBIGUOUS_BY_CLASS["measure"],)
    assert "adjudicate this tie at ingest warming" in amount.resolution


def test_an_adjudicated_tie_binds_through_the_same_verdict_store(db):
    """The amendment's whole point: no second tie mechanism. The verdict is stored through the
    LIVE surface's own store/key machinery and the V2 path reads it back."""
    _catalog(db, second_amount=True)
    refs = ("public.transactions.amount", "public.transactions.orig_amount")
    rows = {r[0]: r for r in db.execute(
        "SELECT object_ref, definition, ai_summary, semantic_terms FROM graph_node "
        "WHERE catalog_source=%s AND object_ref = ANY(%s)", (SOURCE, list(refs))).fetchall()}
    tied = tuple(TieBreakCandidate(ref=ref, definition=row[1] or "", ai_summary=row[2] or "",
                                   semantic_terms=row[3] or "")
                 for ref, row in sorted(rows.items()))
    operand = next(op for op in PROBE_RECIPE.operands if op.role == "amount")
    key = tie_break_input_hash(
        template_id=f"v2:{PROBE_RECIPE.recipe_id}", need_role=operand.role,
        need_concept=operand.concept, intent=PROBE_RECIPE.business_definition, tied=tied)
    store_tie_break_verdict(db, input_hash=key,
                            ranking=["public.transactions.orig_amount",
                                     "public.transactions.amount"],
                            rationale="the original pre-conversion amount is the economic flow",
                            producer_ref="llm_call:v2-adjudication")
    verdicts = bind_v2_operands(db, PROBE_RECIPE, catalog_source=SOURCE, roles=("data_owner",))
    amount = next(v for v in verdicts if v.role == "amount")
    assert amount.status == "bound"
    assert amount.selected_ref == "public.transactions.orig_amount", \
        "the deliberation, not the alphabet, picked the column"
    assert amount.tie_break_verdict_ref == f"tie_break:{key}", \
        "the ref is the deliberation's content-addressed identity in the SHARED store"
    assert set(amount.tied_refs) == set(refs), "the tie stays honestly recorded on the verdict"


def test_an_optional_tie_is_left_unbound_never_silently_selected(db):
    _catalog(db, second_amount=True)
    optional_amount = tuple(
        replace(op, required=False) if op.role == "amount" else op
        for op in PROBE_RECIPE.operands)
    definition = replace(PROBE_RECIPE, operands=optional_amount)
    amount = next(v for v in bind_v2_operands(db, definition, catalog_source=SOURCE,
                                              roles=("data_owner",)) if v.role == "amount")
    assert amount.status == "unresolved"
    assert amount.selected_ref is None
    assert "never silently selected" in amount.resolution


def test_a_missing_required_operand_is_visible_with_its_reason(db):
    _catalog(db)
    db.execute("DELETE FROM graph_node WHERE object_ref = 'public.transactions.dc_flag'")
    direction = next(v for v in bind_v2_operands(db, PROBE_RECIPE, catalog_source=SOURCE,
                                                 roles=("data_owner",)) if v.role == "direction")
    assert direction.status == "unresolved"
    assert direction.reason_codes == (REQUIRED_OPERAND_MISSING,)


def test_concept_compatibility_never_satisfies_an_economic_role(db):
    """The deposit-balance-as-drawn-exposure class: an economic role binds ONLY over
    human-confirmed evidence — with none, the operand is BLOCKED and the resolution says who
    can fix it."""
    _catalog(db)
    demanding = tuple(
        replace(op, economic_role="settlement_flow") if op.role == "amount" else op
        for op in PROBE_RECIPE.operands)
    definition = replace(PROBE_RECIPE, operands=demanding)
    amount = next(v for v in bind_v2_operands(db, definition, catalog_source=SOURCE,
                                              roles=("data_owner",)) if v.role == "amount")
    assert amount.status == "blocked"
    assert amount.reason_codes == (ECONOMIC_ROLE_UNPROVEN,)
    assert "no governed economic-role evidence" in amount.resolution

    _confirm_economic_role(db, "public.transactions.amount", "settlement_flow")
    assert governed_economic_role(db, SOURCE, "public.transactions.amount") == "settlement_flow"
    amount = next(v for v in bind_v2_operands(db, definition, catalog_source=SOURCE,
                                              roles=("data_owner",)) if v.role == "amount")
    assert amount.status == "bound", "human-confirmed evidence — and only that — satisfies it"


def test_a_wrong_confirmed_role_still_blocks(db):
    _catalog(db)
    _confirm_economic_role(db, "public.transactions.amount", "deposit_balance_flow")
    demanding = tuple(
        replace(op, economic_role="drawn_credit_exposure_flow") if op.role == "amount" else op
        for op in PROBE_RECIPE.operands)
    amount = next(v for v in bind_v2_operands(db, replace(PROBE_RECIPE, operands=demanding),
                                              catalog_source=SOURCE, roles=("data_owner",))
                  if v.role == "amount")
    assert amount.status == "blocked"
    assert "deposit_balance_flow" in amount.resolution, \
        "the rejection names what the column IS, not just what it is not"


def test_opposing_legs_refuse_one_physical_column_without_a_sign_authority(db):
    _catalog(db)
    legs = (
        replace(PROBE_RECIPE.operands[1], role="inflow", distinct_binding_group="flow_legs",
                sign_direction_expectation=""),
        replace(PROBE_RECIPE.operands[1], role="outflow", distinct_binding_group="flow_legs",
                sign_direction_expectation=""),
        *PROBE_RECIPE.operands[2:], PROBE_RECIPE.operands[0])
    definition = replace(PROBE_RECIPE, operands=legs)
    verdicts = bind_v2_operands(db, definition, catalog_source=SOURCE, roles=("data_owner",))
    flow_legs = [v for v in verdicts if v.role in ("inflow", "outflow")]
    assert all(v.status == "blocked" for v in flow_legs)
    assert all(v.reason_codes == (DISTINCT_BINDING_VIOLATED,) for v in flow_legs)

    signed = tuple(replace(op, sign_direction_expectation="policy:dc-sign-convention")
                   if op.role in ("inflow", "outflow") else op for op in legs)
    verdicts = bind_v2_operands(db, replace(PROBE_RECIPE, operands=signed),
                                catalog_source=SOURCE, roles=("data_owner",))
    assert all(v.status == "bound" for v in verdicts if v.role in ("inflow", "outflow")), \
        "a governed sign authority is exactly what licenses one column to carry both directions"


def test_a_concept_mismatch_never_binds_at_all(db):
    """The settlement-vs-authorization class, structurally: matching is the SAME two-tier matcher
    grounding uses, so a column of one concept simply is not a candidate for another."""
    _catalog(db)
    wrong = tuple(
        replace(op, concept="mcc") if op.role == "direction" else op
        for op in PROBE_RECIPE.operands)
    direction = next(v for v in bind_v2_operands(db, replace(PROBE_RECIPE, operands=wrong),
                                                 catalog_source=SOURCE, roles=("data_owner",))
                     if v.role == "direction")
    assert direction.status == "unresolved", \
        "dc_flag (debit_credit_indicator) is not a candidate for an mcc operand — no candidate is"


def test_new_operand_spec_fields_are_hash_bearing():
    from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
    demanding = tuple(
        replace(op, economic_role="settlement_flow") if op.role == "amount" else op
        for op in PROBE_RECIPE.operands)
    assert (canonical_recipe_v2_hash(PROBE_RECIPE)
            != canonical_recipe_v2_hash(replace(PROBE_RECIPE, operands=demanding)))
