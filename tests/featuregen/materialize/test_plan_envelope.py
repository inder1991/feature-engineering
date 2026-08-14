"""Task B3 — compilation CONSUMES the frozen plan envelope (design decision D-4).

Before this task the governed plan the human was shown and the plan that would execute were two
independent derivations and nothing compared them: ``fold_frozen_binding_plan`` folded one from the
bound operands, and ``compile_ir`` re-derived the source relation, the PIT gate and the read set
from the formula's own refs plus live governed catalog facts. This module is the comparison.

**The law is VALIDATE, never SUBSTITUTE.** Every check here runs on a COMPLETE IR and can only
return a refusal; nothing in ``materialize/`` reads a value out of the envelope and uses it. That is
the same law ``fold_frozen_binding_plan`` applies to itself with ``BINDING_PLAN_DIVERGENCE``.

**A CORRECTION TO THE TASK, recorded here because it changes what one acceptance test can be.**
B3 asks for ``test_the_compiled_pit_clause_is_the_frozen_pit_text``. There is no compiled PIT
clause: ``PitSpec`` carries structured fields and renders no text anywhere in ``materialize/``,
while ``envelope["pit"]`` is the human-facing PROSE ``recipe_temporal_v2.compile_temporal`` writes
(*"trailing 90d observation window over posted_ts events: (cutoff − 90d, cutoff], values knowable
strictly at or before the cutoff"*). Re-parsing that prose to manufacture a comparison would be
exactly the second derivation D-4 forbids. What the two sides genuinely share is the WINDOW — the
envelope froze it as a number, the compiler compiled it as ``window_length`` — so
``test_the_compiled_window_is_the_frozen_window`` is that test, and the frozen prose is carried
verbatim into the refusal so an operator reads what the human was told.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_ir import (
    _ROLES,
    CUSTOMERS,
    CUSTOMERS_ASOF,
    CUSTOMERS_CIF,
    DECLARATION,
    INVENTORY,
    PUBLIC_FEATURE,
    TXN,
    TXN_CIF,
    TXN_MERCHANT,
    TXN_POSTED,
    _admitted,
    _ok,
    seed_catalog,
)

from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.ir import (
    FormulaExecutionIRV1,
    compile_ir,
    physical_read_set,
)

#: The envelope's own dialect: the semantic context's OBJECT refs, with the catalog carried ONCE
#: beside them. Not the compiler's ``hdfc::public.transactions.merchant_id`` — that difference is
#: the whole reason ``_envelope_ref`` exists, and a test written in the compiler's dialect would
#: pass without ever exercising the translation.
_FROZEN_READ_SET = [
    "public.transactions.merchant_id",       # the operand the recipe's measure role bound
    "public.transactions.txn_dt",            # its event_timestamp role
    "public.transactions.cif_id",            # its entity_key role
]

#: The plan `fold_frozen_binding_plan` would freeze for the worked feature — its nine real keys.
ENVELOPE = {
    "plan_kind": "single_source",
    "catalog_source": fixtures.SOURCE,
    "source_table": "transactions",
    "population_ref": "customers",
    "read_set": list(_FROZEN_READ_SET),
    "role_bindings": {"merchant": "public.transactions.merchant_id"},
    "pit": ("trailing 90d observation window over posted_ts events: (cutoff - 90d, cutoff], "
            "values knowable strictly at or before the cutoff"),
    "output_grain": "customer",
    "window": 90,
}


@pytest.fixture
def catalog(db):
    return seed_catalog(db)


def _compile(catalog, *, envelope=ENVELOPE, feature=PUBLIC_FEATURE, formula=None):
    return compile_ir(
        catalog, _admitted(feature, formula=formula), roles=_ROLES, spine_decl=DECLARATION,
        inventory=INVENTORY, plan_envelope=envelope)


def _refused(value) -> MaterializationRefused:
    assert isinstance(value, MaterializationRefused), value
    return value


# ── the headline property ────────────────────────────────────────────────────────────────────────


def test_the_compiled_read_set_is_the_frozen_read_set(catalog):
    """Every column the compilation resolves is either a column the human's plan named or one of
    the three structurally-explained additions. Nothing else — and the assertion is on the SET, so
    a widening cannot hide inside a passing compile."""
    ir = _ok(_compile(catalog))
    from featuregen.materialize.ir import _allowed_additions, _envelope_ref

    frozen = {_envelope_ref(fixtures.SOURCE, ref) for ref in _FROZEN_READ_SET}
    compiled = {_envelope_ref(fixtures.SOURCE, ref)
                for ref in physical_read_set([ir], ir.spine)}
    assert compiled - frozen - _allowed_additions(ir, fixtures.SOURCE) == set()
    # ...and the frozen refs really are IN the compiled set, so the subset is not vacuous.
    assert frozen <= compiled


def test_the_allowed_additions_are_exactly_the_spine_keys(catalog):
    """PINNED, so the subset carve-out cannot widen silently. These are the only refs a
    compilation may read that the human's plan did not name, and every one of them has a reason
    that is a property of the platform rather than of this fixture:

    the spine's relation, its ordered key and its availability column (the population is a
    separate governed declaration, §4, and the envelope names it by TABLE only); the expression's
    availability gate (§8 rule 1 — a governed catalog fact, never a role the recipe bound); and the
    expression's source relation (which admission's check 7 already compared against
    ``source_table``, in the envelope's own vocabulary).

    A new class of addition must be argued for and added here, in the open."""
    ir = _ok(_compile(catalog))
    from featuregen.materialize.ir import _allowed_additions, _envelope_ref

    assert _allowed_additions(ir, fixtures.SOURCE) == {
        _envelope_ref(fixtures.SOURCE, CUSTOMERS),        # the spine's relation
        _envelope_ref(fixtures.SOURCE, CUSTOMERS_CIF),    # its ordered key / read set
        _envelope_ref(fixtures.SOURCE, CUSTOMERS_ASOF),   # its availability column
        _envelope_ref(fixtures.SOURCE, TXN_POSTED),       # the expression's availability gate
        _envelope_ref(fixtures.SOURCE, TXN),              # the expression's source relation
    }


def test_a_column_the_plan_never_named_refuses_by_name(catalog):
    """The check has teeth: drop one genuinely-read column from the frozen read set and the same
    compilation refuses, naming the column and the plan."""
    narrowed = {**ENVELOPE,
                "read_set": [ref for ref in _FROZEN_READ_SET if "merchant_id" not in ref]}
    refusal = _refused(_compile(catalog, envelope=narrowed))
    assert refusal.code is CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE
    assert "merchant_id" in refusal.detail


def test_the_compiled_window_is_the_frozen_window(catalog):
    """The PIT half, in the only vocabulary the two sides share (see the module docstring). The
    frozen prose rides the refusal verbatim; it is never re-parsed."""
    assert _ok(_compile(catalog)).expressions[0].pit.window_length == ENVELOPE["window"]

    refusal = _refused(_compile(catalog, envelope={**ENVELOPE, "window": 30}))
    assert refusal.code is CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE
    assert "90" in refusal.detail and "30" in refusal.detail
    assert ENVELOPE["pit"] in refusal.detail


def test_a_divergent_population_refuses_by_name(catalog):
    """The spine is validated against the governed facts and then compared with the population the
    human's card declared."""
    refusal = _refused(
        _compile(catalog, envelope={**ENVELOPE, "population_ref": "somewhere_else"}))
    assert refusal.code is CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE
    assert "somewhere_else" in refusal.detail and CUSTOMERS in refusal.detail


# ── admission's half (check 7), and the "nothing was rendered" property ──────────────────────────


def test_a_divergent_source_table_refuses_by_name(catalog):
    """The artifact-shaped half of D-4, at ADMISSION — the earliest point at which it is knowable,
    and therefore the point at which nothing downstream has been produced.

    ``compile_ir`` is never reached, so no IR, no authorization, no contract and no rendered
    project exist: the assertion is not merely that a code came back but that the pipeline stopped
    before it could make anything.
    """
    from featuregen.materialize.admission import _verify_plan_envelope

    formula = fixtures.authored_formula(PUBLIC_FEATURE)
    with pytest.raises(MaterializationRefused) as excinfo:
        _verify_plan_envelope(formula, {**ENVELOPE, "source_table": "somewhere_else"}, "run-0001")
    assert excinfo.value.code is CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE
    assert "somewhere_else" in excinfo.value.detail
    assert "transactions" in excinfo.value.detail


def test_a_divergent_grain_refuses_at_admission(catalog):
    """The other half of check 7 — and the reason it is at admission rather than at compile: the
    grain is a property of the ARTIFACT, knowable without resolving anything."""
    from featuregen.materialize.admission import _verify_plan_envelope

    formula = fixtures.authored_formula(PUBLIC_FEATURE)
    with pytest.raises(MaterializationRefused) as excinfo:
        _verify_plan_envelope(formula, {**ENVELOPE, "output_grain": "merchant"}, "run-0001")
    assert excinfo.value.code is CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE
    assert "merchant" in excinfo.value.detail and "customer" in excinfo.value.detail


def test_a_grain_that_differs_only_in_CASE_is_not_a_divergence(catalog):
    """The catalog says ``Customer``, the planning grain says ``customer``. A capitalization is not
    a different unit of analysis — the same rule ``fold_frozen_binding_plan`` applies to the UOA."""
    from featuregen.materialize.admission import _verify_plan_envelope

    formula = fixtures.authored_formula(PUBLIC_FEATURE)
    _verify_plan_envelope(formula, {**ENVELOPE, "output_grain": "  CUSTOMER "}, "run-0001")


# ── absence asserts nothing ──────────────────────────────────────────────────────────────────────


def test_a_run_without_an_envelope_compiles_exactly_as_before(catalog):
    """THE REGRESSION GUARD on the whole phase. Every work item written before migration 1068
    carries no envelope, and the entire materialization path predates it; a compilation with no
    envelope must produce the IDENTICAL object, field for field and hash for hash."""
    from featuregen.materialize.ir import ir_hash

    without = _ok(compile_ir(catalog, _admitted(PUBLIC_FEATURE), roles=_ROLES,
                             spine_decl=DECLARATION, inventory=INVENTORY))
    empty = _ok(compile_ir(catalog, _admitted(PUBLIC_FEATURE), roles=_ROLES,
                           spine_decl=DECLARATION, inventory=INVENTORY, plan_envelope={}))
    matching = _ok(_compile(catalog))
    assert without == empty == matching
    assert ir_hash(without) == ir_hash(matching)


def test_a_blank_field_in_the_envelope_asserts_nothing(catalog):
    """A field the envelope left empty is not a claim compilation must satisfy — ``window`` is
    ``None`` for a recipe with no window parameter, ``output_grain`` is ``""`` for a planning
    request that declared none. Reading absence as an assertion would refuse the majority of
    genuine features for having been served by an earlier build."""
    hollow = {"plan_kind": "single_source", "catalog_source": "", "source_table": "",
              "population_ref": "", "read_set": [], "role_bindings": {}, "pit": "",
              "output_grain": "", "window": None}
    assert isinstance(_compile(catalog, envelope=hollow), FormulaExecutionIRV1)


def test_the_envelope_never_substitutes_a_compiled_value(catalog):
    """D-4 stated as a property of the OUTPUT, not of the code: an envelope that disagrees can only
    produce a refusal, and one that agrees produces the same IR as no envelope at all. There is no
    third outcome in which a compiled value came from the envelope."""
    ir = _ok(_compile(catalog))
    baseline = _ok(compile_ir(catalog, _admitted(PUBLIC_FEATURE), roles=_ROLES,
                              spine_decl=DECLARATION, inventory=INVENTORY))
    assert ir == baseline
    for mutated in ({**ENVELOPE, "population_ref": "elsewhere"},
                    {**ENVELOPE, "window": 7},
                    {**ENVELOPE, "read_set": ["public.transactions.cif_id"]}):
        assert _refused(_compile(catalog, envelope=mutated)).code is (
            CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE)


def test_a_relation_ref_is_not_read_as_a_column(catalog):
    """``_envelope_ref``'s one subtlety, pinned. A table ref has two path segments; shifting it one
    segment left would name a table ``transactions`` in a schema ``public`` — which compares equal
    to nothing and unequal to everything, and would make the read-set check silently useless."""
    from featuregen.materialize.ir import _envelope_ref

    assert _envelope_ref("hdfc", TXN) == ("hdfc", "public", "transactions", "")
    assert _envelope_ref("hdfc", TXN_CIF) == ("hdfc", "public", "transactions", "cif_id")
    # The two dialects meet: a bare object ref and its governed logical ref are ONE address.
    assert _envelope_ref("hdfc", "public.transactions.merchant_id") == _envelope_ref(
        "", TXN_MERCHANT)


def test_a_ratio_body_is_checked_on_EVERY_expression(catalog):
    """The window check iterates the expressions rather than trusting the first: a ratio has two,
    and a divergence in the denominator is as much a divergence as one in the numerator."""
    ratio = fixtures.authored_formula("cross_border_value_ratio_90d")
    envelope = {**ENVELOPE,
                "read_set": [*_FROZEN_READ_SET, "public.transactions.txn_amt",
                             "public.transactions.cross_border_flag",
                             "public.transactions.dr_cr_flag", "public.transactions.status_cd"],
                "window": 30}
    refusal = _refused(_compile(catalog, feature="cross_border_value_ratio_90d",
                                formula=dataclasses.replace(ratio), envelope=envelope))
    assert refusal.code is CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE
    assert "body." in refusal.detail


def test_the_chain_hands_compile_ir_the_envelope_the_gate_admitted(catalog):
    """THE WIRING, asserted structurally — because the alternative is a phase that validates
    nothing in production while every unit test passes.

    ``compile_feature_group`` must pass the envelope carried on the ADMITTED feature, not re-read
    it from the work item or from the resolved input: admission's check 7 and ``compile_ir``'s
    checks have to be about ONE object, or a divergence could clear one gate and be invisible to
    the other. Read from the module's AST, the same way ``test_admission`` proves the gate accepts
    no bare formula and ``materialization_runs`` proves the route never compiles.
    """
    import ast
    import inspect

    from featuregen.materialize.compile import chain as chain_module

    tree = ast.parse(inspect.getsource(chain_module))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "compile_ir"]
    assert len(calls) == 1, "the chain compiles in exactly one place"
    keywords = {kw.arg: ast.unparse(kw.value) for kw in calls[0].keywords}
    assert keywords.get("plan_envelope") == "feature.plan_envelope"
