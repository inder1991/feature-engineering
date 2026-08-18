"""The shared leaves have a home that is not v1's module, and v2/v3 do not reach into v1.

This is step 0 of the V2-only plan, and it exists to make step 15 a deletion rather than an
excavation. `formula/schema.py` is v1's module by name, but it also held every structural leaf the
newer languages import — refs, filters, literals, parameters, grains, window units, decimal policy.
So "delete v1" broke v2, and nobody could tell from the module names.

The leaves now live in `schema_leaves`. These tests pin the two properties that make the split
worth having, both stated as things that must stay FALSE.
"""
from __future__ import annotations

import ast
import pathlib


def _imports_from(module: str, target: str) -> set[str]:
    """Names `module` imports from `target`, read from the source rather than at runtime.

    Source rather than `__import__`, because a re-export would make a runtime check pass while the
    dependency it is about still exists in the code.
    """
    path = pathlib.Path("src/featuregen/formula") / module
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module == target:
            names |= {alias.name for alias in node.names}
    return names


def test_V2_AND_V3_DO_NOT_IMPORT_FROM_V1S_MODULE():
    """The property the whole split exists for.

    While this was false, "remove v1" began with working out what would break, and the answer was
    "the two languages that replaced it". Now v1 is removable by deletion.
    """
    for module in ("schema_v2.py", "schema_v3.py"):
        leaked = _imports_from(module, "featuregen.formula.schema")
        assert not leaked, f"{module} still imports from v1's module: {sorted(leaked)}"


def test_the_leaves_module_carries_NO_VERSIONED_VOCABULARY():
    """The other half: the shared home must not become a second place v1 lives.

    An aggregate function, a final operation, a body shape or a proposal is *the language*. If one
    of those appears here, some later version will import it and the split will have bought nothing.
    """
    source = (pathlib.Path("src/featuregen/formula/schema_leaves.py")).read_text()
    defined = {
        node.name
        for node in ast.parse(source).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
    }
    versioned = {
        "AggregateFunction", "FinalOperation", "AggregateExpression", "UnaryBody", "RatioBody",
        "DiffBody", "ExpectedOutput", "TypedFormulaProposalV1", "FormulaOutputPolicyV1",
        "TypedFormulaV1", "validate_semantics",
    }
    assert not (defined & versioned), sorted(defined & versioned)


def test_the_leaves_are_THE_SAME_OBJECTS_not_copies():
    """Duplicating them would fork validation the grammars must share.

    v1 re-exports from the shared module rather than keeping its own definitions, so there is
    exactly one `FilterNode` in the process and a fix to filter validation cannot land for one
    language and miss the other.
    """
    from featuregen.formula import schema, schema_leaves, schema_v2

    for name in ("FilterNode", "NullInput", "EmptyWindowResult", "DecimalPolicy", "Grain"):
        canonical = getattr(schema_leaves, name)
        assert getattr(schema, name) is canonical, f"{name} forked from v1"
        if hasattr(schema_v2, name):
            assert getattr(schema_v2, name) is canonical, f"{name} forked from v2"
