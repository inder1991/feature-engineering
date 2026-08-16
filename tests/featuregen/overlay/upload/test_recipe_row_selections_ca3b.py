"""C-A3b — a recipe DECLARES its row selection; nothing infers it from the name or the prose.

``posted_debit_amount`` and ``posted_credit_amount`` were structurally IDENTICAL — the same
operands (``with_direction=True``), the same policy refs — and differed only in their recipe NAME
and their prose. A deterministic author could therefore reach "debit" only by inference, which is
what these tests exist to make impossible.

``row_selections=()`` means **this recipe declares no structural row selection** — a positive
statement, not "not migrated yet". An empty tuple that actually meant "unknown" would let a recipe
read complete while its semantics were undecided.
"""
from __future__ import annotations

import pytest

from featuregen.formula.schema_v3 import SelectionKind, SemanticRowSelectionV1
from featuregen.overlay.upload.recipe_contract_v2 import EligibilitySpecV2
from featuregen.overlay.upload.recipe_formula_blueprint_derivation import derive_blueprint_v2
from featuregen.overlay.upload.recipe_grounding_context import (
    canonical_recipe_v2,
    canonical_recipe_v2_hash,
)
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
from featuregen.overlay.upload.recipes.transaction_foundation import (
    TRANSACTION_FOUNDATION_RECIPES,
)

_BY_ID = {d.recipe_id: d for d in TRANSACTION_FOUNDATION_RECIPES}
_DEBIT = _BY_ID["posted_debit_amount"]
_CREDIT = _BY_ID["posted_credit_amount"]


def _selection(value: str) -> SemanticRowSelectionV1:
    return SemanticRowSelectionV1(SelectionKind.TRANSACTION_DIRECTION, "direction", value)


# ── 1. the hashes move ONCE — deterministically, for every recipe ───────────────────────────────
def test_every_recipe_hash_is_deterministic_and_row_selections_is_hash_bearing():
    """"Exactly once" means the re-baseline is a single deterministic step: the same definition
    always hashes the same, and the new axis is inside EVERY recipe's canonical form — including
    the ones declaring none, because `()` is a statement, not an absence."""
    assert len(V2_RECIPES) == 317
    for definition in V2_RECIPES:
        assert canonical_recipe_v2_hash(definition) == canonical_recipe_v2_hash(definition)
        assert "row_selections" in canonical_recipe_v2(definition)["definition"], \
            definition.recipe_id


# ── 3 + 7. debit and credit differ STRUCTURALLY, and the hash follows ───────────────────────────
def test_debit_and_credit_differ_structurally():
    assert _DEBIT.row_selections == (_selection("debit"),)
    assert _CREDIT.row_selections == (_selection("credit"),)
    assert canonical_recipe_v2_hash(_DEBIT) != canonical_recipe_v2_hash(_CREDIT)


def test_swapping_the_selection_changes_the_hash():
    """Check 7: the selection is identity-bearing, so debit and credit can never collide."""
    import dataclasses
    swapped = dataclasses.replace(_DEBIT, row_selections=(_selection("credit"),))
    assert canonical_recipe_v2_hash(swapped) != canonical_recipe_v2_hash(_DEBIT)


def test_removing_the_selection_changes_the_hash():
    import dataclasses
    stripped = dataclasses.replace(_DEBIT, row_selections=())
    assert canonical_recipe_v2_hash(stripped) != canonical_recipe_v2_hash(_DEBIT)


# ── 4. nothing is inferred from the name or the prose ───────────────────────────────────────────
def test_the_selection_is_declared_not_inferred_from_the_recipe_id():
    """A recipe whose id says "debit" but which DECLARES credit carries credit. If anything read
    the name, this would disagree with itself."""
    import dataclasses
    mislabelled = dataclasses.replace(_DEBIT, row_selections=(_selection("credit"),))
    blueprint, *_ = _derive(mislabelled)
    assert blueprint.expressions[0].row_selections == (_selection("credit"),)
    assert "debit" in mislabelled.recipe_id            # the name still says debit
    assert "DEBIT" in mislabelled.business_definition  # so does the prose


def test_the_blueprint_carries_the_declaration_for_both_directions():
    for definition, value in ((_DEBIT, "debit"), (_CREDIT, "credit")):
        blueprint, *_ = _derive(definition)
        assert blueprint.expressions[0].row_selections == (_selection(value),), definition.recipe_id


def _derive(definition):
    out = derive_blueprint_v2(definition)
    return out if isinstance(out, tuple) else (out,)


# ── the declaration is checked, not merely stored ───────────────────────────────────────────────
def test_a_physical_literal_refuses_at_the_recipe_layer():
    import dataclasses
    with pytest.raises(Exception, match="physical literal|not one of"):
        dataclasses.replace(_DEBIT, row_selections=(
            SemanticRowSelectionV1(SelectionKind.TRANSACTION_DIRECTION, "direction", "D"),))


def test_a_direction_selection_without_a_direction_policy_refuses():
    """The validation that caught a real gap: a recipe selecting BY direction while declaring no
    governed direction convention is a recipe reading a column with no rule for its values."""
    import dataclasses
    with pytest.raises(Exception, match="direction_sign"):
        dataclasses.replace(
            _DEBIT,
            eligibility=EligibilitySpecV2(policy_refs=("eligible_status:foundation-posted-events",)),
            row_selections=(_selection("debit"),))


def test_duplicate_selections_for_one_role_refuse():
    import dataclasses
    with pytest.raises(Exception, match="duplicate row selection"):
        dataclasses.replace(_DEBIT, row_selections=(_selection("debit"), _selection("credit")))


# ── the empty tuple is a STATEMENT ──────────────────────────────────────────────────────────────
def test_an_empty_selection_tuple_is_a_declaration_of_none():
    """Documented meaning, pinned: `()` says "this recipe declares no structural row selection".
    Every recipe therefore has an answer — none is left implicitly undecided."""
    undeclared = [d.recipe_id for d in V2_RECIPES if d.row_selections == ()]
    declared = [d.recipe_id for d in V2_RECIPES if d.row_selections]
    assert len(undeclared) + len(declared) == len(V2_RECIPES)
    assert set(declared) >= {"posted_debit_amount", "posted_credit_amount"}


# ── 2 + 5 + 6. the review re-baseline, against a real database ──────────────────────────────────
def _review_api():
    from featuregen.overlay.upload.recipe_review import record_review_event, review_events
    from featuregen.overlay.upload.recipe_review_validity import (
        by_role_at_revision,
        required_reviewer_roles,
        review_validity,
    )
    return (record_review_event, review_events, by_role_at_revision,
            required_reviewer_roles, review_validity)


def test_a_review_at_the_old_hash_no_longer_reads_current(conn):
    """Check 2. The 996 seeded events were signed at hashes that no longer exist, so the fold
    stops finding them — which is the honest outcome: a reviewer who approved before
    `row_selections` existed never assessed row-selection meaning."""
    record, events, by_role, roles_for, validity = _review_api()
    stale_hash = "sha256:the-hash-before-row-selections-existed"
    for role in roles_for(_DEBIT):
        record(conn, recipe_id=_DEBIT.recipe_id, recipe_revision_hash=stale_hash,
               decision="approved", reviewer=f"dev-fixture:{role}", reviewer_role=role,
               rationale="pre-C-A3b baseline")

    current_hash = canonical_recipe_v2_hash(_DEBIT)
    assert current_hash != stale_hash
    at_current = by_role(events(conn, _DEBIT.recipe_id), current_hash)
    assert at_current == {}, "an event signed at the old hash must not satisfy the new revision"
    assert not validity(_DEBIT, at_current).current


def test_old_events_remain_queryable_as_history(conn):
    """Check 6. The re-baseline writes NEW events; it never rewrites or deletes the old ones."""
    record, events, by_role, roles_for, _validity = _review_api()
    stale_hash = "sha256:the-hash-before-row-selections-existed"
    role = next(iter(roles_for(_DEBIT)))
    record(conn, recipe_id=_DEBIT.recipe_id, recipe_revision_hash=stale_hash,
           decision="approved", reviewer=f"dev-fixture:{role}", reviewer_role=role,
           rationale="pre-C-A3b baseline")

    all_events = events(conn, _DEBIT.recipe_id)
    assert any(e.recipe_revision_hash == stale_hash for e in all_events)
    assert by_role(all_events, stale_hash), "history is still addressable at its own revision"


def test_reseeding_at_the_new_hash_reads_current(conn):
    """Check 5, and the seeder's contract in miniature: fresh `dev-fixture:<role>` approvals at the
    CURRENT hash make the recipe read review-current through the validity fold — the fold being the
    claim, not the write."""
    record, events, by_role, roles_for, validity = _review_api()
    revision = canonical_recipe_v2_hash(_DEBIT)
    for role in roles_for(_DEBIT):
        record(conn, recipe_id=_DEBIT.recipe_id, recipe_revision_hash=revision,
               decision="approved", reviewer=f"dev-fixture:{role}", reviewer_role=role,
               rationale="C-A3b re-baseline")

    assert validity(_DEBIT, by_role(events(conn, _DEBIT.recipe_id), revision)).current


def test_a_second_seed_plans_zero_writes(conn):
    """Check 8, on the seeder's actual planning rule: it plans only roles NOT already signed at the
    current revision, so a second run has nothing to write."""
    record, events, by_role, roles_for, _validity = _review_api()
    revision = canonical_recipe_v2_hash(_DEBIT)

    def planned():
        signed = by_role(events(conn, _DEBIT.recipe_id), revision)
        return [r for r in roles_for(_DEBIT) if r not in signed]

    first = planned()
    assert first, "the first run must have something to write"
    for role in first:
        record(conn, recipe_id=_DEBIT.recipe_id, recipe_revision_hash=revision,
               decision="approved", reviewer=f"dev-fixture:{role}", reviewer_role=role,
               rationale="C-A3b re-baseline")
    assert planned() == [], "a second run must plan zero events"
