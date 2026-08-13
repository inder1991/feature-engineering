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


def _flow_legs(sign_expectation: str = "", *, with_direction: bool = False):
    """Two opposing monetary legs in one distinct group. PROBE_RECIPE's own direction operand
    (debit_credit_indicator) is EXCLUDED by default — it is itself a licensing representation
    (leg one of the C3 sign law), so isolating the sign_convention path requires its absence."""
    direction = (PROBE_RECIPE.operands[2],) if with_direction else ()
    return (
        replace(PROBE_RECIPE.operands[1], role="inflow", distinct_binding_group="flow_legs",
                sign_direction_expectation=sign_expectation),
        replace(PROBE_RECIPE.operands[1], role="outflow", distinct_binding_group="flow_legs",
                sign_direction_expectation=sign_expectation),
        *direction, PROBE_RECIPE.operands[3], PROBE_RECIPE.operands[0])


def _sign_convention_evidence(db, object_ref: str, *, producer: str, strength: str) -> None:
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.column_authority import logical_ref_of

    logical = logical_ref_of(db, SOURCE, object_ref)
    record_field_evidence(
        db, logical_ref=logical, field_name="sign_convention",
        proposed_value="signed_amount", producer=producer, strength=strength,
        producer_ref=f"{producer}:test", source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="sign_convention",
                                    material="signed_amount"))


def test_opposing_legs_refuse_one_physical_column_without_a_sign_representation(db):
    """C3 (validated correction): the AUTHORED sign_direction_expectation is an EXPECTATION,
    never authority — authoring a string on the recipe cannot license one column to carry
    both directions. Only the catalog's OWN governed sign representation can."""
    _catalog(db)
    verdicts = bind_v2_operands(db, replace(PROBE_RECIPE, operands=_flow_legs()),
                                catalog_source=SOURCE, roles=("data_owner",))
    flow_legs = [v for v in verdicts if v.role in ("inflow", "outflow")]
    assert all(v.status == "blocked" for v in flow_legs)
    assert all(v.reason_codes == (DISTINCT_BINDING_VIOLATED,) for v in flow_legs)

    # The pre-C3 defect: an authored expectation string used to LICENSE the bind. It must not.
    signed = _flow_legs(sign_expectation="policy:dc-sign-convention")
    verdicts = bind_v2_operands(db, replace(PROBE_RECIPE, operands=signed),
                                catalog_source=SOURCE, roles=("data_owner",))
    flow_legs = [v for v in verdicts if v.role in ("inflow", "outflow")]
    assert all(v.status == "blocked" for v in flow_legs), \
        "an authored expectation is not evidence — the catalog must carry the representation"
    assert "sign_convention" in flow_legs[0].resolution
    assert "debit_credit_indicator" in flow_legs[0].resolution


def test_a_confirmed_sign_convention_on_the_shared_column_licenses_the_legs(db):
    _catalog(db)
    _sign_convention_evidence(db, "public.transactions.amount",
                              producer="human", strength="confirmed")
    verdicts = bind_v2_operands(db, replace(PROBE_RECIPE, operands=_flow_legs()),
                                catalog_source=SOURCE, roles=("data_owner",))
    assert all(v.status == "bound" for v in verdicts if v.role in ("inflow", "outflow")), \
        "a GOVERNED signed-amount convention is exactly what licenses both directions"


def test_a_proposed_sign_convention_is_not_authority(db):
    _catalog(db)
    _sign_convention_evidence(db, "public.transactions.amount",
                              producer="llm", strength="proposed")
    verdicts = bind_v2_operands(db, replace(PROBE_RECIPE, operands=_flow_legs()),
                                catalog_source=SOURCE, roles=("data_owner",))
    assert all(v.status == "blocked" for v in verdicts
               if v.role in ("inflow", "outflow")), \
        "a proposal retrieves; it never clears the authoring floor"


def test_a_bound_direction_operand_licenses_the_legs(db):
    """The OTHER governed representation real banking schemas use: positive magnitudes plus a
    debit/credit indicator column — bound in the SAME recipe, no sign fact needed. This is
    PROBE_RECIPE's own shape (its direction operand binds the catalog's dc_flag)."""
    _catalog(db)
    verdicts = bind_v2_operands(
        db, replace(PROBE_RECIPE, operands=_flow_legs(with_direction=True)),
        catalog_source=SOURCE, roles=("data_owner",))
    by_role = {v.role: v for v in verdicts}
    assert by_role["direction"].status == "bound"
    assert by_role["inflow"].status == "bound" and by_role["outflow"].status == "bound"


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


# ── C6: rank before truncating — authority wins over position ──────────────────────────────────

def _wide_catalog(db, n: int = 25):
    """N same-concept amount columns; the LAST one (index 20+ in ref order) gets the human
    confirmation. Pre-C6 the stable-ref cut at 16 dropped it before anyone looked."""
    rows = [(CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                          entity="Account"), "account_id"),
            (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp"),
             "event_timestamp"),
            (CanonicalRow(SOURCE, "transactions", "dc_flag", "text"),
             "debit_credit_indicator")]
    for i in range(n):
        rows.append((CanonicalRow(SOURCE, "transactions", f"amt_{i:02d}", "numeric",
                                  additivity="additive", currency="USD"), "monetary_flow"))
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _confirm_concept(db, object_ref: str, concept: str) -> None:
    logical = logical_ref_of(db, SOURCE, object_ref)
    record_field_evidence(
        db, logical_ref=logical, field_name="concept", proposed_value=concept,
        producer="human", strength="confirmed", producer_ref="user:sme",
        source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                    material=concept))


def _bind_probe(db):
    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_recipe,
    )
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_operand_policy import bind_planning_request

    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    request = planning_request_from_recipe(PROBE_RECIPE)
    return bind_planning_request(db, request, context)


def test_a_confirmed_column_at_index_20_of_25_wins_the_shortlist(db):
    """C6's acceptance: authority ranks BEFORE the cut, so the human-confirmed column deep
    in ref order enters the shortlist and WINS — and the verdict records the truncation."""
    _wide_catalog(db, 25)
    _confirm_concept(db, "public.transactions.amt_20", "monetary_flow")
    verdicts, eligibility = _bind_probe(db)
    amount = next(v for v in verdicts if v.role == "amount")
    assert amount.status == "bound"
    assert amount.selected_ref == "public.transactions.amt_20"
    assert amount.shortlist_truncated is True


def test_truncation_survives_to_the_observation_row(db):
    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_recipe,
        planning_request_hash,
    )
    from featuregen.overlay.upload.recipe_planning_lens import V2RecipeCandidateV1
    from featuregen.overlay.upload.semantic_candidate_store import (
        persist_semantic_candidates,
    )

    _wide_catalog(db, 25)
    _confirm_concept(db, "public.transactions.amt_20", "monetary_flow")
    verdicts, eligibility = _bind_probe(db)
    request = planning_request_from_recipe(PROBE_RECIPE)
    candidate = V2RecipeCandidateV1(
        recipe_id=PROBE_RECIPE.recipe_id, relationship="primary",
        planning_request=request, planning_request_hash=planning_request_hash(request),
        recipe_revision_hash="rev", verdicts=verdicts,
        binding_state="bound", readiness="FORMULA_BLOCKED",
        temporal_pit_text="pit", temporal_blocker="",
        review_current=False, review_missing_roles=(), eligibility=eligibility)
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    persist_semantic_candidates(
        db, generation_run_id="fgr_c6_probe", context=context, candidates=[candidate])
    rows = db.execute(
        "SELECT verdicts FROM semantic_candidate_observation "
        "WHERE generation_run_id = 'fgr_c6_probe'").fetchall()
    assert rows
    stored = [v for row in rows for v in row[0] if v["role"] == "amount"]
    assert stored and stored[0]["shortlist_truncated"] is True


def test_the_hint_promotes_an_eligible_ref_and_cannot_promote_a_blocked_one(db):
    """C6 rule 4: the user's binding hint is a RANKING signal — it orders retrieval among
    peers; it never overrides eligibility (a blocked column stays blocked, hinted or not)."""
    from featuregen.overlay.upload.feature_planning_contracts import (
        RequiredOperandV1,
        planning_request_from_user_definition,
    )
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_operand_policy import bind_planning_request

    _wide_catalog(db, 25)
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    exemplar = PROBE_RECIPE.operands[1]
    request = planning_request_from_user_definition(
        definition_id="user:hint_probe", primary_objective=PROBE_RECIPE.primary_objective,
        output=PROBE_RECIPE.output,
        operands=(RequiredOperandV1(
            role="amount", concept="monetary_flow", operand_class="measure",
            unit_expectation=exemplar.unit_expectation,
            binding_hint_refs=("public.transactions.amt_22",)),),
        source_grain="transaction", output_grain="account",
        temporal=PROBE_RECIPE.temporal, content_hash="hinthash")
    verdicts, _ = bind_planning_request(db, request, context)
    amount = next(v for v in verdicts if v.role == "amount")
    assert amount.status == "bound"
    assert amount.selected_ref == "public.transactions.amt_22", \
        "equal-authority peers — the user's hint orders retrieval"

    # The hinted ref is structurally BLOCKED (an identifier hinted as a measure): the hint
    # cannot promote it — eligibility still decides.
    request2 = planning_request_from_user_definition(
        definition_id="user:hint_probe2", primary_objective=PROBE_RECIPE.primary_objective,
        output=PROBE_RECIPE.output,
        operands=(RequiredOperandV1(
            role="amount", concept="monetary_flow", operand_class="measure",
            alternative_concepts=("account_id",),
            binding_hint_refs=("public.transactions.acct_ref",)),),
        source_grain="transaction", output_grain="account",
        temporal=PROBE_RECIPE.temporal, content_hash="hinthash2")
    verdicts, _ = bind_planning_request(db, request2, context)
    amount = next(v for v in verdicts if v.role == "amount")
    assert amount.selected_ref != "public.transactions.acct_ref"


# ── C7: semantic closure — a registered descendant retrieves and binds; a mate does not ────────

def test_an_operand_retrieves_and_binds_a_registered_descendant_concept(db):
    """C7's acceptance: `interest_income` IS-A `monetary_flow` in the registry — a column
    enriched with the descendant retrieves for the ancestor operand AND binds (the meaning
    matches by construction); a genuinely different meaning still refuses."""
    rows = [(CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                          entity="Account"), "account_id"),
            (CanonicalRow(SOURCE, "transactions", "int_income", "numeric",
                          additivity="additive", currency="USD"), "interest_income"),
            (CanonicalRow(SOURCE, "transactions", "dc_flag", "text"),
             "debit_credit_indicator"),
            (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp"),
             "event_timestamp")]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    verdicts, eligibility = _bind_probe(db)
    amount = next(v for v in verdicts if v.role == "amount")
    assert amount.status == "bound", (amount.status, amount.reason_codes)
    assert amount.selected_ref == "public.transactions.int_income", \
        "the registered descendant IS the requested meaning"


def test_a_namespace_mate_is_retrieval_only_never_a_meaning_substitute(db):
    """C7: `counterparty_id` shares customer_id's namespace (join candidacy) but is NOT a
    meaning substitute — retrieved into the audit, refused by eligibility (CONCEPT_MISMATCH)."""
    from featuregen.overlay.upload import semantic_eligibility_reasons as R
    from featuregen.overlay.upload.feature_planning_contracts import (
        RequiredOperandV1,
        planning_request_from_user_definition,
    )
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_operand_policy import bind_planning_request

    rows = [(CanonicalRow(SOURCE, "trades", "cpty_ref", "varchar(20)"), "counterparty_id"),
            (CanonicalRow(SOURCE, "trades", "booked_ts", "timestamp"), "event_timestamp")]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    context = build_generation_semantic_context(db, catalog_source=SOURCE)
    request = planning_request_from_user_definition(
        definition_id="user:mate_probe", primary_objective=PROBE_RECIPE.primary_objective,
        output=PROBE_RECIPE.output,
        operands=(RequiredOperandV1(role="who", concept="customer_id",
                                    operand_class="entity_key"),),
        source_grain="transaction", output_grain="customer",
        temporal=PROBE_RECIPE.temporal, content_hash="matehash")
    verdicts, eligibility = bind_planning_request(db, request, context)
    who = next(v for v in verdicts if v.role == "who")
    assert who.status != "bound", "a join-candidacy peer never binds as the meaning"
    mate = eligibility.get(("who", "public.trades.cpty_ref"))
    assert mate is not None, "the mate WAS retrieved — visible in the audit"
    assert mate.status == "not_applicable"
    assert R.CONCEPT_MISMATCH in mate.reason_codes


def test_closure_changes_move_the_context_hash(db):
    """C7: the closure is context CONTENT — enriching a column with a descendant concept
    changes what retrieval can see, so it must be a NEW context identity."""
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )

    rows = [(CanonicalRow(SOURCE, "transactions", "amount", "numeric",
                          additivity="additive", currency="USD"), "monetary_flow")]
    build_graph(db, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    before = build_generation_semantic_context(db, catalog_source=SOURCE)
    assert "monetary_flow" in before.concept_closure
    assert "monetary_flow" in before.concept_closure["monetary_flow"]

    rows2 = [(CanonicalRow(SOURCE, "transactions", "int_inc", "numeric",
                           additivity="additive", currency="USD"), "interest_income")]
    build_graph(db, SOURCE, [r for r, _ in rows2],
                concepts={content_hash(r): c for r, c in rows2})
    after = build_generation_semantic_context(db, catalog_source=SOURCE)
    assert "monetary_flow" in after.concept_closure["interest_income"]
    assert after.context_hash() != before.context_hash()


# ── C9: the declared history depth through the REAL binder — surgical per variant ──────────────

def _declare_history_depth(db, table: str, days: int, *, producer="source",
                           strength="attested") -> None:
    """The upload-manifest declaration lands as source/attested — the evidence vocabulary's
    structurally-vouched tier (the plan's "source/declared" in matrix terms)."""
    from featuregen.overlay.upload.object_ref import normalize_ref

    logical = normalize_ref(SOURCE, "public", table, None)
    record_field_evidence(
        db, logical_ref=logical, field_name="history_depth_days", proposed_value=str(days),
        producer=producer, strength=strength, producer_ref=f"{producer}:test",
        source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="history_depth_days",
                                    material=str(days)))


def test_a_window_exceeding_the_declared_depth_blocks_that_variant_only(db):
    """C9 both directions: with 90 days declared, the 180-day request blocks by name and the
    30-day request binds — and with NOTHING declared, both stay byte-identical to today."""
    from dataclasses import replace as _replace

    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_recipe,
    )
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_operand_policy import bind_planning_request

    _catalog(db)

    def _bind(window):
        context = build_generation_semantic_context(db, catalog_source=SOURCE)
        request = planning_request_from_recipe(PROBE_RECIPE)
        request = _replace(request, parameter_values=(("window", window),))
        return bind_planning_request(db, request, context)

    # Nothing declared: both windows bind exactly as today.
    for window in (30, 180):
        verdicts, _ = _bind(window)
        event = next(v for v in verdicts if v.role == "event_ts")
        assert event.status == "bound", (window, event.reason_codes)

    _declare_history_depth(db, "transactions", 90)
    verdicts, _ = _bind(180)
    event = next(v for v in verdicts if v.role == "event_ts")
    assert event.status == "blocked"
    assert "HISTORY_DEPTH_INSUFFICIENT" in event.reason_codes

    verdicts, _ = _bind(30)
    event = next(v for v in verdicts if v.role == "event_ts")
    assert event.status == "bound", "the shorter variant of the SAME recipe stays eligible"


def test_a_stronger_correction_clears_a_previously_blocked_variant(db):
    """C9: corrections are append-only NEW rows, never overwrites — a human confirming the
    real depth (400d) outranks the source's 90d declaration, and the 180-day variant that
    was blocked clears on the next bind."""
    from dataclasses import replace as _replace

    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_recipe,
    )
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_operand_policy import bind_planning_request

    _catalog(db)
    _declare_history_depth(db, "transactions", 90)

    def _bind_180():
        context = build_generation_semantic_context(db, catalog_source=SOURCE)
        request = _replace(planning_request_from_recipe(PROBE_RECIPE),
                           parameter_values=(("window", 180),))
        verdicts, _ = bind_planning_request(db, request, context)
        return next(v for v in verdicts if v.role == "event_ts")

    assert _bind_180().status == "blocked"
    _declare_history_depth(db, "transactions", 400, producer="human", strength="confirmed")
    assert _bind_180().status == "bound", \
        "the stronger append-only correction wins through the resolver pin"
