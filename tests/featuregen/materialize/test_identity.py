"""Spec A Task 11 — §7's TWO-PHASE identity, and why the phases cannot be one object.

**Non-circularity is the whole task.** Rev 3 put `generated_project_hash` inside the identity that
the rendered files embed — so the identity was an input to the bytes whose hash it contained. The
fix is two phases: `CompilationIdentity` is COMPLETE before rendering and is what the rendered files
carry; `RenderedArtifactIdentity` is built afterwards, from bytes that already exist. The project
hash is computed over every generated file EXCEPT `GENERATED.lock` and is written only into that
lock — `test_the_project_hash_lives_in_the_lock_and_in_NO_other_file` is the assertion that keeps
the self-reference from coming back through the door it was pushed out of.

**Plurality.** A group holds several features, so `formula_content_hashes` and `ir_hashes` are
tuples. An earlier draft had them singular, which silently meant "the first feature's" — and an
identity that names one of three features identifies a group that was never compiled.

**Sandbox only, structurally.** `derive_namespace()` takes no parameters, so there is no argument a
production namespace could arrive on, and the module spells no namespace literal at all: it returns
`binding.SANDBOX_NAMESPACE`, so the two cannot drift.
`test_derive_namespace_DERIVES_the_namespace_rather_than_re_spelling_it` proves that behaviourally
by moving the binding's constant — an `is` comparison would prove nothing, because CPython interns
an identifier-shaped literal and a re-spelled `"sandbox_feature"` would be the same object.

**Run-time values are the defect the split exists to prevent.** A partition value or a business date
inside `CompilationIdentity` would make the generated project change every day. Two independent
proofs here: the four fields are pinned with `==`, and the canonical BYTES of the payload are
searched for every run-time literal the execution hash is given (with a positive control, so a
search that found nothing because it cannot find anything would fail).
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import pathlib
import types

import pytest
from tests.featuregen.materialize.test_group_plan import (  # noqa: F401 — `catalog` is a fixture
    COUNT_90D,
    RATIO_90D,
    SUM_30D,
    _real_plan,
    catalog,
)
from tests.featuregen.materialize.test_ir import _compile, _ok

from featuregen.formula._jcs import dumps as jcs_dumps
from featuregen.materialize import binding, compile, identity, render
from featuregen.materialize.binding import physical_target_for
from featuregen.materialize.group_plan import group_plan_hash
from featuregen.materialize.identity import (
    GENERATED_LOCK_FILENAME,
    CompilationIdentity,
    RenderedArtifactIdentity,
    SealedProject,
    build_compilation_identity,
    derive_namespace,
    generated_project_hash,
    read_lock,
    sandbox_execution_hash,
    seal_project,
)
from featuregen.materialize.ir import ir_hash

# ── the values a RUN carries, and which no compilation-phase object may ever see ─────────────────

ENVIRONMENT = "hdfc-local"
BUSINESS_DT = "2026-07-27"
ATTESTATION = "att-0001"
SNAPSHOTS = ("snap-transactions-0001", "snap-customers-0001")
PARAMETERS = {"transactions_partitions": ["load_dt=2026-07-26", "load_dt=2026-07-27"],
              "business_dt": BUSINESS_DT}

#: Every literal above that a run supplies, as bytes — searched for inside the compilation-phase
#: payloads. Kept as one list so a value added to the execution hash is added to the proof too.
RUN_TIME_LITERALS = (BUSINESS_DT, ENVIRONMENT, ATTESTATION, *SNAPSHOTS,
                     "load_dt=2026-07-27")

#: The parameter NAMES §7 puts on the run side. A compilation-phase entry point that named one of
#: these would be taking a run-time value however carefully its body then ignored it.
#: `renderer_version` is deliberately NOT here. Task 12 removed the parameter: the value is read
#: from `render.RENDERER_VERSION`, so there is no argument on which a stale literal could arrive.
#: `compiler_version` is deliberately NOT here either. Task 15 removed that parameter the same way,
#: once `featuregen.materialize.compile` existed to own `COMPILER_VERSION`.
RUN_TIME_PARAMETERS = frozenset({
    "environment_id", "parameters", "business_dt", "input_snapshot_ids",
    "capability_attestation_id"})

FILES = {
    "pyproject.toml": "[project]\nname = \"generated_cif_daily\"\n",
    "requirements.lock": "pyspark==3.4.1\nkedro==0.19.3\n",
    "conf/base/catalog.yml": "transactions:\n  type: spark.SparkDataset\n",
    "src/generated_cif_daily/settings.py": "HOOKS = ()\n",
    "src/generated_cif_daily/pipelines/materialize/nodes.py": "def assemble(spine):\n    return spine\n",
    "README.md": "# generated\n\n`kedro run` inside a Spark session.\n",
}


def _compilation(**overrides) -> CompilationIdentity:
    fields = {
        "formula_content_hashes": ("formula-count", "formula-sum"),
        "ir_hashes": ("ir-count", "ir-sum"),
        "materialization_contract_hash": "contract-0001",
        "group_plan_hash": "plan-0001",
    }
    fields.update(overrides)
    return CompilationIdentity(**fields)  # type: ignore[arg-type]


def _sealed(files=None, compilation=None) -> SealedProject:
    return seal_project(compilation or _compilation(), FILES if files is None else files)


def _exec(**overrides) -> str:
    fields = {
        "rendered": _sealed().identity,
        "environment_id": ENVIRONMENT,
        "parameters": PARAMETERS,
        "business_dt": BUSINESS_DT,
        "input_snapshot_ids": SNAPSHOTS,
        "capability_attestation_id": ATTESTATION,
    }
    fields.update(overrides)
    rendered = fields.pop("rendered")
    return sandbox_execution_hash(rendered, **fields)  # type: ignore[arg-type]


def _code_string_literals(module: types.ModuleType) -> set[str]:
    """Every string constant in the module's CODE — docstrings deliberately excluded.

    Prose may name the sandbox namespace (the module docstring does); code may not, because a code
    literal is the thing that drifts from `binding.SANDBOX_NAMESPACE`.
    """
    tree = ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings}


# ══ §7 — the two phases are two objects ══════════════════════════════════════════════════════════


def test_the_compilation_identity_has_only_static_compilation_fields() -> None:
    """Pinned with `==`, not `<=`: a superset assertion would let `generated_project_hash` be added
    back tomorrow, which is precisely the circularity rev 3 shipped.  Exact directional bridge
    dependencies and crosswalk execution pins are static compilation inputs and are the final
    revalidation handle."""
    assert {field.name for field in dataclasses.fields(CompilationIdentity)} == {
        "formula_content_hashes", "ir_hashes", "materialization_contract_hash", "group_plan_hash",
        "bridge_realization_dependencies", "crosswalk_execution_pins"}


def test_the_rendered_identity_is_the_compilation_PLUS_the_project_hash() -> None:
    assert {field.name for field in dataclasses.fields(RenderedArtifactIdentity)} == {
        "compilation", "generated_project_hash"}


def test_the_rendered_identity_cannot_be_built_without_a_compilation() -> None:
    """It is built AFTER rendering, on top of a compilation that was already complete — so a
    caller cannot mint one from a bare string and skip the first phase entirely."""
    with pytest.raises(TypeError):
        RenderedArtifactIdentity(compilation="contract-0001",  # type: ignore[arg-type]
                                 generated_project_hash="a" * 64)


def test_the_compilation_identity_is_complete_BEFORE_anything_is_rendered() -> None:
    """No file, no project hash and no renderer is needed to build it — which is what makes the
    rendered files able to embed it."""
    assert set(inspect.signature(build_compilation_identity).parameters) == {"irs", "plan"}
    assert _compilation().identity_payload()["group_plan_hash"] == "plan-0001"


# ══ §7 — the project hash covers every file EXCEPT the lock ══════════════════════════════════════


def test_the_lock_is_excluded_from_the_project_hash() -> None:
    """The exclusion is what makes the identity non-circular: the lock is written FROM the hash, so
    a hash computed over it could never be reproduced. `generated_project_hash` therefore answers
    the same value before and after the lock exists — which is how L0 verifies a complete project."""
    sealed = _sealed()
    assert generated_project_hash(FILES) == sealed.identity.generated_project_hash
    assert generated_project_hash(sealed.files) == sealed.identity.generated_project_hash


def test_a_DIFFERENT_lock_does_not_move_the_project_hash() -> None:
    forged = {**FILES, GENERATED_LOCK_FILENAME: '{"generated_project_hash": "not this"}'}
    assert generated_project_hash(forged) == generated_project_hash(FILES)


@pytest.mark.parametrize("path", sorted(FILES))
def test_EVERY_other_file_is_covered(path: str) -> None:
    """A file the hash does not cover is a file an operator could edit without detection."""
    edited = {**FILES, path: FILES[path] + "# edited\n"}
    assert generated_project_hash(edited) != generated_project_hash(FILES)


def test_a_removed_file_moves_the_project_hash() -> None:
    remaining = {path: text for path, text in FILES.items() if path != "README.md"}
    assert generated_project_hash(remaining) != generated_project_hash(FILES)


def test_a_RENAMED_file_moves_the_project_hash() -> None:
    """Paths are identity, not just contents: the same bytes at another path is another project."""
    renamed = {path: text for path, text in FILES.items() if path != "README.md"}
    renamed["docs/README.md"] = FILES["README.md"]
    assert generated_project_hash(renamed) != generated_project_hash(FILES)


def test_an_added_file_moves_the_project_hash() -> None:
    assert generated_project_hash({**FILES, "extra.py": "x = 1\n"}) != generated_project_hash(FILES)


def test_the_project_hash_lives_in_the_lock_and_in_NO_other_file() -> None:
    """§7: *every other generated file must be free of it*. The list is compared to the lock's name
    rather than merely checked for absence elsewhere, so the same assertion also proves the lock
    really does carry it — a search that finds it nowhere at all would otherwise pass."""
    sealed = _sealed()
    holders = sorted(path for path, text in sealed.files.items()
                     if sealed.identity.generated_project_hash in text)
    assert holders == [GENERATED_LOCK_FILENAME]


def test_sealing_refuses_a_renderer_supplied_lock() -> None:
    """The lock is MINTED from the hash. A renderer that wrote its own would either be stating a
    hash it could not know or be feeding one back into the bytes being hashed."""
    with pytest.raises(ValueError):
        seal_project(_compilation(), {**FILES, GENERATED_LOCK_FILENAME: "{}"})


def test_the_sealed_project_is_the_rendered_files_plus_the_lock() -> None:
    sealed = _sealed()
    assert set(sealed.files) == set(FILES) | {GENERATED_LOCK_FILENAME}
    assert all(sealed.files[path] == text for path, text in FILES.items())


def test_the_sealed_file_set_cannot_be_mutated() -> None:
    """The lock describes the files as they were hashed; a set that could still be edited would let
    the lock become a statement about a project that no longer exists."""
    with pytest.raises(TypeError):
        _sealed().files["src/generated_cif_daily/settings.py"] = "HOOKS = (1,)\n"  # type: ignore[index]


def test_the_lock_round_trips_the_rendered_identity() -> None:
    """One format, read by L0 and by the rendered assembly node (§10.2). A second parser would be a
    second chance to disagree about which key holds the project hash."""
    sealed = _sealed()
    assert read_lock(sealed.files[GENERATED_LOCK_FILENAME]) == sealed.identity


def test_cross_catalog_lock_round_trips_exact_bridge_dependencies() -> None:
    compilation = _compilation(
        bridge_realization_dependencies=(
            ("brr-2", "brds-2"),
            ("brr-1", "brds-1"),
            ("brr-1", "brds-1"),
        ),
    )
    sealed = seal_project(compilation, FILES)
    restored = read_lock(sealed.files[GENERATED_LOCK_FILENAME])
    assert restored == sealed.identity
    assert restored.compilation.bridge_realization_dependencies == (
        ("brr-1", "brds-1"),
        ("brr-2", "brds-2"),
    )


def test_a_lock_missing_the_project_hash_is_refused() -> None:
    """The compilation half alone is the FIRST phase — a lock that stops there records no project.

    The compilation it carries is COMPLETE on purpose. A lock whose compilation was *also*
    malformed is refused by the inner check, and this assertion would then pass while the top-level
    one went untested — which is exactly how a mutation that deleted the top-level check survived.
    """
    with pytest.raises(ValueError):
        read_lock(json.dumps({"compilation": _compilation().identity_payload()}))


def test_a_lock_carrying_an_EXTRA_key_is_refused() -> None:
    """The lock holds EXACTLY a rendered identity. A business date riding in it would be a run-time
    value inside a generated file — the very thing the phase split exists to keep out."""
    smuggled = {**_sealed().identity.identity_payload(), "business_dt": BUSINESS_DT}
    with pytest.raises(ValueError):
        read_lock(json.dumps(smuggled))


def test_a_lock_whose_COMPILATION_is_incomplete_is_refused() -> None:
    with pytest.raises(ValueError):
        read_lock('{"compilation": {}, "generated_project_hash": "' + "a" * 64 + '"}')


def test_sealing_is_deterministic() -> None:
    first, second = _sealed(), _sealed()
    assert first.identity == second.identity
    assert first.files[GENERATED_LOCK_FILENAME] == second.files[GENERATED_LOCK_FILENAME]


def test_a_project_of_no_files_is_refused() -> None:
    with pytest.raises(ValueError):
        generated_project_hash({})


def test_a_project_of_ONLY_a_lock_is_refused() -> None:
    """The lock is excluded, so this is the empty project wearing a manifest."""
    with pytest.raises(ValueError):
        generated_project_hash({GENERATED_LOCK_FILENAME: "{}"})


@pytest.mark.parametrize("path", ["/etc/passwd", "../escape.py", "src\\windows.py", "./x.py", ""])
def test_a_path_that_is_not_INSIDE_the_project_is_refused(path: str) -> None:
    """Paths enter identity, so they must denote one file each: `..` escapes the project and two
    spellings of one path would hash as two files."""
    with pytest.raises(ValueError):
        generated_project_hash({**FILES, path: "x = 1\n"})


def test_file_contents_must_be_TEXT() -> None:
    with pytest.raises(TypeError):
        generated_project_hash({**FILES, "blob.bin": b"\x00"})  # type: ignore[dict-item]


# ══ plurality — a group has MANY features ════════════════════════════════════════════════════════


def test_the_hashes_are_PLURAL() -> None:
    """Singular fields would silently mean "the first feature's", and an identity naming one of
    three features identifies a group nobody compiled."""
    compilation = _compilation(formula_content_hashes=("f-a", "f-b", "f-c"),
                               ir_hashes=("ir-a", "ir-b", "ir-c"))
    assert compilation.formula_content_hashes == ("f-a", "f-b", "f-c")
    assert compilation.ir_hashes == ("ir-a", "ir-b", "ir-c")
    payload = compilation.identity_payload()
    assert payload["formula_content_hashes"] == ["f-a", "f-b", "f-c"]
    assert payload["ir_hashes"] == ["ir-a", "ir-b", "ir-c"]


def test_a_repeated_formula_hash_is_KEPT() -> None:
    """Two features may be authored from one formula. De-duplicating would report a two-feature
    group as a one-feature group and break the positional pairing with `ir_hashes`."""
    compilation = _compilation(formula_content_hashes=("f-a", "f-a"), ir_hashes=("ir-a", "ir-b"))
    assert compilation.identity_payload()["formula_content_hashes"] == ["f-a", "f-a"]


def test_the_two_tuples_must_be_the_same_LENGTH() -> None:
    """They are positionally paired — one formula per feature, one IR per feature."""
    with pytest.raises(ValueError):
        _compilation(formula_content_hashes=("f-a",), ir_hashes=("ir-a", "ir-b"))


def test_an_identity_naming_no_feature_is_refused() -> None:
    with pytest.raises(ValueError):
        _compilation(formula_content_hashes=(), ir_hashes=())


@pytest.mark.parametrize("field", ["formula_content_hashes", "ir_hashes"])
def test_a_blank_member_hash_is_refused(field: str) -> None:
    with pytest.raises(ValueError):
        _compilation(**{field: ("ir-a", "  ")})


@pytest.mark.parametrize("field", ["materialization_contract_hash", "group_plan_hash"])
def test_a_blank_group_hash_is_refused(field: str) -> None:
    with pytest.raises(ValueError):
        _compilation(**{field: ""})


def test_a_LIST_of_hashes_is_coerced_to_a_tuple() -> None:
    """A frozen dataclass holding a list is a frozen object with a mutable field."""
    assert _compilation(ir_hashes=["ir-a", "ir-b"]).ir_hashes == ("ir-a", "ir-b")


def test_a_bare_STRING_of_hashes_is_refused() -> None:
    """A single hash passed unwrapped iterates per CHARACTER — the shape that has bitten this
    codebase before, and it would produce a 6-feature identity from one feature."""
    with pytest.raises(TypeError):
        _compilation(ir_hashes="ir-a")


# ══ sandbox only, structurally ═══════════════════════════════════════════════════════════════════


def test_derive_namespace_takes_NO_parameters() -> None:
    """Rev 4 removed a gate that unlocked `feature.*` on two non-empty strings. With no parameter
    there is no argument a production namespace could arrive on."""
    assert inspect.signature(derive_namespace).parameters == {}


def test_derive_namespace_DERIVES_the_namespace_rather_than_re_spelling_it() -> None:
    """Task 10's requirement: it returns `binding.SANDBOX_NAMESPACE`, so the two cannot drift.

    Proven by MOVING the binding's constant. An `is` comparison would prove nothing — CPython
    interns identifier-shaped literals, so a re-spelled `"sandbox_feature"` would be the very same
    object as the binding's.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(binding, "SANDBOX_NAMESPACE", "some_other_namespace")
        assert derive_namespace() == "some_other_namespace"


def test_the_module_spells_NO_namespace_literal_at_all() -> None:
    """Docstrings may name the sandbox namespace; code may not. The first assertion is the control:
    a scanner that returned nothing would pass the real check vacuously."""
    literals = _code_string_literals(identity)
    assert GENERATED_LOCK_FILENAME in literals, "the literal scanner cannot see code literals"
    assert [literal for literal in literals if "sandbox_feature" in literal] == []
    assert literals & {"feature", "feature_store", "prod", "prod_feature", "production",
                       "production_feature"} == set()


def test_the_namespace_is_the_one_the_binding_publishes_to() -> None:
    """Pinned BY VALUE on one side: `derive_namespace() == SANDBOX_NAMESPACE` would only assert
    that the module agrees with itself."""
    assert derive_namespace() == "sandbox_feature"
    assert physical_target_for("cif_daily") == "sandbox_feature.cif_daily"


def test_there_is_no_production_path() -> None:
    """`__all__` is pinned with `==`, so a `production_namespace` cannot be added quietly."""
    assert set(identity.__all__) == {
        "GENERATED_LOCK_FILENAME", "CompilationIdentity", "RenderedArtifactIdentity",
        "SealedProject", "build_compilation_identity", "derive_namespace",
        "generated_project_hash", "read_lock", "sandbox_execution_hash", "seal_project"}
    assert [name for name in dir(identity) if "produc" in name.lower()] == []


def test_there_is_no_production_execution_hash_and_no_bare_execution_hash() -> None:
    """§7: the reduced identity is never recorded as `execution_hash`. A name that dropped the
    `sandbox_` prefix would read as an execution identity that had a production counterpart."""
    assert not hasattr(identity, "production_execution_hash")
    assert not hasattr(identity, "execution_hash")


# ══ §7 — what the SANDBOX EXECUTION hash covers ══════════════════════════════════════════════════


def test_the_execution_hash_covers_everything_section_7_names() -> None:
    """The compilation identity and the project hash arrive together as the RENDERED identity, so
    a caller cannot pair one compilation with another project's hash."""
    params = inspect.signature(sandbox_execution_hash).parameters
    assert set(params) == {"rendered"} | set(RUN_TIME_PARAMETERS)
    assert [name for name, param in params.items()
            if param.default is not inspect.Parameter.empty] == [], \
        "a defaulted parameter would let a caller omit the attestation id and still get a hash"


@pytest.mark.parametrize("field, value", [
    ("environment_id", "hdfc-uat"),
    ("business_dt", "2026-07-28"),
    ("capability_attestation_id", "att-0002"),
    ("input_snapshot_ids", ("snap-transactions-0002", "snap-customers-0001")),
    ("parameters", {**PARAMETERS, "business_dt": "2026-07-28"}),
])
def test_EVERY_covered_value_moves_the_execution_hash(field: str, value: object) -> None:
    assert _exec(**{field: value}) != _exec()


def test_the_RENDERER_version_is_read_rather_than_accepted_and_still_moves_the_hash() -> None:
    """Task 12. §7 covers the renderer's version, and the one thing this hash exists to detect is a
    renderer that changed — so a call site free to pass a literal could freeze it at whatever it
    typed while the renderer moved underneath. There is no parameter, and the value is read THROUGH
    the module, which is what this proves by moving the constant."""
    assert "renderer_version" not in inspect.signature(sandbox_execution_hash).parameters
    baseline = _exec()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(render, "RENDERER_VERSION", "renderer-9.9.9")
        assert _exec() != baseline


def test_the_COMPILER_version_is_read_rather_than_accepted_and_still_moves_the_hash() -> None:
    """Task 15, closing A.13's other half. §7 covers the compiler's version for the same reason it
    covers the renderer's, and a parameter is a place a caller can freeze one. `compile` now owns
    `COMPILER_VERSION`, so there is no argument left to pass — proved by moving the constant."""
    assert "compiler_version" not in inspect.signature(sandbox_execution_hash).parameters
    baseline = _exec()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(compile, "COMPILER_VERSION", "compiler-9.9.9")
        assert _exec() != baseline


@pytest.mark.parametrize("package", [render, compile])
def test_the_version_owning_package_init_imports_NOTHING(package: types.ModuleType) -> None:
    """`identity` reads both versions out of these packages, and both have (or will have) a module
    that imports `identity` back — `render.project` seals, and §2's chain builds a compilation
    identity. An import added to either `__init__` closes the loop, and the failure would surface as
    an ImportError in whichever module was imported first."""
    source = pathlib.Path(package.__file__).read_text(encoding="utf-8")
    parsed = ast.parse(source)
    assert [node for node in ast.walk(parsed)
            if isinstance(node, ast.Import | ast.ImportFrom)
            and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")] == []


def test_a_different_PROJECT_moves_the_execution_hash() -> None:
    other = _sealed({**FILES, "README.md": "# other\n"}).identity
    assert _exec(rendered=other) != _exec()


def test_a_different_COMPILATION_moves_the_execution_hash() -> None:
    other = _sealed(compilation=_compilation(group_plan_hash="plan-0002")).identity
    assert _exec(rendered=other) != _exec()


def test_the_ORDER_of_the_snapshot_ids_is_identity() -> None:
    """§3.4 calls `input_snapshot_ids` *the exact ordered partition set read*. Sorting them here
    would declare two different read orders equivalent, which is not this module's judgement to
    make — run preparation resolves them per expression and owns their order."""
    assert _exec(input_snapshot_ids=tuple(reversed(SNAPSHOTS))) != _exec()


def test_the_execution_hash_is_reproducible() -> None:
    assert _exec() == _exec()
    assert _exec(input_snapshot_ids=list(SNAPSHOTS)) == _exec()


def test_the_parameter_KEY_ORDER_is_not_identity() -> None:
    """RFC 8785 sorts object keys, so the same resolved parameters hash identically whichever order
    run preparation happened to assemble them in."""
    reordered = dict(reversed(list(PARAMETERS.items())))
    assert _exec(parameters=reordered) == _exec()


def test_the_execution_hash_is_a_sha256_hex_digest() -> None:
    digest = _exec()
    assert len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def test_a_run_that_names_NO_input_is_refused() -> None:
    """Every Spec A run reads at least the spine, so an empty tuple is a caller who resolved
    nothing — and it would hash the same as a run whose inputs nobody recorded."""
    with pytest.raises(ValueError):
        _exec(input_snapshot_ids=())


def test_a_snapshot_id_list_given_as_a_bare_STRING_is_refused() -> None:
    with pytest.raises(TypeError):
        _exec(input_snapshot_ids="snap-transactions-0001")


@pytest.mark.parametrize("field", ["environment_id", "business_dt",
                                   "capability_attestation_id"])
def test_a_blank_run_value_is_refused(field: str) -> None:
    with pytest.raises(ValueError):
        _exec(**{field: "   "})


def test_the_execution_hash_requires_a_RENDERED_identity() -> None:
    """A `CompilationIdentity` alone would be an execution hash for a project that was never
    rendered — and the project hash is exactly what makes two runs of two builds distinguishable."""
    with pytest.raises(TypeError):
        _exec(rendered=_compilation())


def test_the_parameters_must_be_a_mapping() -> None:
    with pytest.raises(TypeError):
        _exec(parameters=[("business_dt", BUSINESS_DT)])


# ══ no run-time value reaches the compilation phase ══════════════════════════════════════════════


def test_no_run_time_LITERAL_appears_in_the_compilation_identity_bytes() -> None:
    """The byte-level proof. The control asserts the search finds a value that IS present, so a
    search incapable of finding anything cannot pass the real assertion."""
    blob = jcs_dumps(_compilation().identity_payload())
    assert b"ir-sum" in blob, "the byte search cannot find a value that is present"
    for literal in RUN_TIME_LITERALS:
        assert literal.encode() not in blob, literal


def test_no_run_time_LITERAL_appears_in_the_lock_either() -> None:
    """The lock is a generated file. A business date in it would change the project every day."""
    lock = _sealed().files[GENERATED_LOCK_FILENAME]
    assert "ir-sum" in lock, "the search cannot find a value that is present"
    for literal in RUN_TIME_LITERALS:
        assert literal not in lock, literal


def test_the_compilation_phase_names_no_run_time_PARAMETER() -> None:
    """Structural companion to the byte proof: a compilation-phase entry point that accepted
    `business_dt` would be taking a run-time value however carefully its body ignored it."""
    for function in (build_compilation_identity, generated_project_hash, seal_project,
                     derive_namespace, read_lock):
        assert set(inspect.signature(function).parameters) & RUN_TIME_PARAMETERS == set(), function
    assert RUN_TIME_PARAMETERS <= set(inspect.signature(sandbox_execution_hash).parameters)


def test_the_compilation_payload_names_no_run_time_KEY() -> None:
    payload = _compilation().identity_payload()
    assert set(payload) & (RUN_TIME_PARAMETERS | {"generated_project_hash"}) == set()


# ══ the real compilation, really identified ══════════════════════════════════════════════════════


def test_a_real_compilation_identifies_the_real_plan(catalog, db) -> None:  # noqa: F811
    """The composition test: Task 7's IRs and Task 10's plan reach one identity with no adapter."""
    plan = _real_plan(db, SUM_30D, COUNT_90D)
    irs = [_ok(_compile(db, name)) for name in (SUM_30D, COUNT_90D)]
    compilation = build_compilation_identity(irs, plan)
    assert compilation.ir_hashes == tuple(feature.ir_hash for feature in plan.features)
    assert compilation.materialization_contract_hash == plan.materialization_contract_hash
    assert compilation.group_plan_hash == group_plan_hash(plan)
    assert set(compilation.formula_content_hashes) == {ir.formula_content_hash for ir in irs}


def test_the_ORDER_the_features_compiled_in_does_not_change_the_identity(catalog, db) -> None:  # noqa: F811
    """The plan is ordered by column name, so the identity is too — a group is not two groups
    because someone compiled its members the other way round."""
    plan = _real_plan(db, SUM_30D, COUNT_90D)
    forwards = [_ok(_compile(db, name)) for name in (SUM_30D, COUNT_90D)]
    assert build_compilation_identity(forwards, plan) \
        == build_compilation_identity(list(reversed(forwards)), plan)


def test_the_paired_tuples_describe_the_SAME_feature_positionally(catalog, db) -> None:  # noqa: F811
    """`formula_content_hashes[i]` and `ir_hashes[i]` are one feature's, so the identity can be
    read without re-fetching the IRs it summarizes."""
    plan = _real_plan(db, SUM_30D, COUNT_90D)
    irs = {ir.feature_name: ir for ir in (_ok(_compile(db, name)) for name in (SUM_30D, COUNT_90D))}
    compilation = build_compilation_identity(list(irs.values()), plan)
    for index, feature in enumerate(plan.features):
        assert compilation.ir_hashes[index] == ir_hash(irs[feature.column_name])
        assert compilation.formula_content_hashes[index] == \
            irs[feature.column_name].formula_content_hash


def test_an_IR_the_plan_does_not_name_is_refused(catalog, db) -> None:  # noqa: F811
    """The same closure Task 10 draws: an IR with no planned column publishes nothing, and a
    planned column with no IR is a column no computation produced."""
    plan = _real_plan(db, COUNT_90D)
    irs = [_ok(_compile(db, name)) for name in (COUNT_90D, RATIO_90D)]
    with pytest.raises(ValueError):
        build_compilation_identity(irs, plan)


def test_a_planned_ir_hash_that_no_IR_produced_is_refused(catalog, db) -> None:  # noqa: F811
    """Nothing else in the chain checks that the plan's `ir_hash` is the compiled IR's — §9 then
    gates staging manifests against a value nobody proved."""
    plan = _real_plan(db, COUNT_90D)
    tampered = dataclasses.replace(
        plan, features=(dataclasses.replace(plan.features[0], ir_hash="ir-" + "0" * 8),))
    with pytest.raises(ValueError):
        build_compilation_identity([_ok(_compile(db, COUNT_90D))], tampered)


def test_the_plan_must_be_a_group_plan(catalog, db) -> None:  # noqa: F811
    """A bare list of features never showed that its members share one materialization contract."""
    with pytest.raises(TypeError):
        build_compilation_identity([_ok(_compile(db, COUNT_90D))], "cif_daily")  # type: ignore[arg-type]
