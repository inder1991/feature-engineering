"""Step 8's structural guard — the defect class, stated once, in a form that cannot drift.

**What went wrong, and why counting it was hard.** ``FinalOperationV2.RATIO is FinalOperation.RATIO``
is ``False`` — different enum object, same name, same value. A V2 ratio did not fail in the renderer;
it took the else-branch and rendered a DIFFERENT operation. The first count of these sites said five,
a review said six, and the truth was ten. A count is not a guard: it is right on the day it is taken.

**And the accidental half is worse than the broken half.** The two enums also compare EQUAL and HASH
EQUAL, so a V2 member looked up in a V1-keyed dispatch table finds the right entry. Dispatch that
works while identity fails is how a renderer emits code down the wrong arm without raising anything.

So the guard is not "no ``is`` against these three names". It is: **the render package does not
import the versioned enums at all.** A name that is not in scope cannot be compared against, keyed
on, or annotated with — and this holds for sites nobody has written yet.

**Which enums are hazardous is DERIVED, never listed.** A V1 enum is hazardous exactly when
``schema_v2`` carries a same-named ``…V2`` twin that is a DIFFERENT object. ``NullInput``,
``EmptyWindowResult`` and ``FilterKind`` are the same objects re-exported — genuinely
version-neutral — and comparing them with ``is`` is correct and stays. A hardcoded list would have
to be remembered the day a fourth enum is versioned; this test simply starts failing.
"""
from __future__ import annotations

import ast
import enum
import inspect
import pathlib

from featuregen.formula import schema, schema_v2
from featuregen.materialize.render import nodes_compute


def _versioned_enum_names() -> set[str]:
    """V1 enum names whose V2 twin is a different object — the ones identity comparison betrays."""
    names = set()
    for name in dir(schema):
        member = getattr(schema, name)
        if not (isinstance(member, type) and issubclass(member, enum.Enum)):
            continue
        twin = getattr(schema_v2, f"{name}V2", None)
        if twin is not None and twin is not member:
            names.add(name)
    return names


def _render_modules() -> list[pathlib.Path]:
    return sorted(pathlib.Path(inspect.getfile(nodes_compute)).parent.rglob("*.py"))


def test_the_versioned_enums_are_the_three_we_think_they_are():
    """Stated so the guard below cannot silently become vacuous.

    A derivation that quietly returned the empty set would make every assertion pass while proving
    nothing — the failure mode of every structural test that computes its own subject.
    """
    assert _versioned_enum_names() == {"AggregateFunction", "FinalOperation", "WindowBasis"}


def test_THE_RENDER_PACKAGE_NEVER_IMPORTS_A_VERSIONED_V1_ENUM():
    """One renderer, one vocabulary, and the crossing happens before anything gets here.

    Not "no `is` comparisons": a name in scope can also be keyed on, annotated with, or passed
    along, and each of those has already been a bug in this package. Absence is the only form of
    this rule that covers the sites nobody has written yet.
    """
    hazardous = _versioned_enum_names()
    offences: list[str] = []
    for path in _render_modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if not node.module.startswith("featuregen.formula"):
                continue
            for alias in node.names:
                if alias.name in hazardous:
                    offences.append(f"{path.name}:{node.lineno} imports {alias.name}")
    assert offences == [], (
        "the render package imports a V1 enum that has a DIFFERENT V2 twin. Its members compare "
        "equal and hash equal to the V2 ones and are not identical, so dispatch keyed on them "
        "silently succeeds while every `is` comparison silently fails:\n  "
        + "\n  ".join(offences))


def test_the_renderer_DOES_still_use_the_version_neutral_enums():
    """The discriminator. A guard that passed because the render package imported nothing from
    `formula` at all would be green and worthless."""
    imported: set[str] = set()
    for path in _render_modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "featuregen.formula"):
                imported.update(alias.name for alias in node.names)

    # These ARE the same objects in both modules, so `is` against them is correct and expected.
    assert {"NullInput", "EmptyWindowResult"} <= imported
    for name in ("NullInput", "EmptyWindowResult"):
        assert getattr(schema, name) is getattr(schema_v2, name)


def test_the_compiled_IR_carries_the_V2_AGGREGATE_and_says_so():
    """The normalization point, asserted on the TYPE rather than on a comment: the field's
    annotation was what let this drift, so the annotation is now checked."""
    from featuregen.materialize.expression_ir import ExpressionExecutionIR

    assert (ExpressionExecutionIR.__annotations__["aggregation"]
            in (schema_v2.AggregateFunctionV2, "AggregateFunctionV2"))


def test_an_operation_the_renderer_CANNOT_EMIT_refuses_by_name():
    """`signed_sum` is in V2's vocabulary and no branch emits it. Converting it successfully and
    letting it match nothing is the fall-through this whole step exists to remove."""
    from featuregen.materialize.render.nodes_compute import _RENDERABLE_OPERATIONS

    assert schema_v2.FinalOperationV2.SIGNED_SUM.value not in _RENDERABLE_OPERATIONS
    assert set(_RENDERABLE_OPERATIONS) == {"identity", "ratio", "difference"}
