"""Spec A Task 14b — §10.2's assembly node and §9's blocking gates, as rendered source.

Two nodes, and the split between them is §9's own: *"run after assembly, before publication"*.

* :func:`render_assembly_node` reduces the per-feature staging outputs onto the spine and adds the
  three system columns **once**, having first judged the evidence that says the staging area belongs
  to this run at all — the staging manifests (§9) and the project's own bytes (``PROJECT_INTEGRITY``).
  Those gates run BEFORE the reduction because a group assembled from another run's staging output
  is not a group anyone asked to be graded.
* :func:`render_gate_node` judges the assembled group's SHAPE — keys, columns, types, nullability,
  forbidden numerics and the schema hash — and hands the frame it validated to the publication
  dataset. It contains no publication mechanism and names no table: the catalog decides where the
  frame goes, and Task 16 replaces that entry with the one its probe attested (§10.3).

**Why the gates are rendered rather than executed here.** Every member of ``ValidationGateCode`` is,
in its own docstring's words, *"discoverable only during execution — failures on real rows"*. A
check that ran in this process would be judging a plan; these judge the frames the cluster actually
produced.

**The code travels as TEXT.** The generated project cannot import ``featuregen``, so a gate raises
``RuntimeError`` whose message BEGINS with the member's value — the convention Task 13a's rendered
gates already use, kept rather than replaced (see :func:`gate_code_of`, which reads it back).

**Findings are collected, then raised together.** ``check_completeness`` returns every failure
rather than the first, because a gate that stopped early sends an operator round the
regenerate/rerun loop once per broken feature; the rendered gates answer the same way, joining their
findings with ``" | "`` so the leading token still names the gate that fired first. That is one
raise per NODE, not one per check — and it is the one place this module's rendered shape differs
from Task 13a's, which raises the instant a single rule is broken because there is nothing else it
could usefully report at that point.

The rendering primitives (``_comment``, ``_wrap``, ``_safe_text``, ``_call_lines``) are imported
from :mod:`~featuregen.materialize.render.nodes_compute` rather than restated. A second copy of the
word-wrapper would be a second answer about how a rendered message is broken across lines, and the
two would drift on the first change to either.
"""
from __future__ import annotations

from collections.abc import Mapping

from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.group_plan import (
    SYSTEM_COLUMNS,
    ColumnRole,
    FeatureGroupPlanV1,
    StagingStatus,
    expected_schema,
    expected_schema_hash,
)
from featuregen.materialize.identity import GENERATED_LOCK_FILENAME
from featuregen.materialize.render.nodes_compute import (
    _LOCK_DEPTH,
    _RENDERED_WIDTH,
    _call_lines,
    _comment,
    _dataset,
    _safe_text,
    _wrap,
)
from featuregen.materialize.render.project import RenderedNode

__all__ = [
    "ASSEMBLY_FUNC_NAME",
    "ASSEMBLY_NODE_NAME",
    "FINDING_SEPARATOR",
    "GATE_FUNC_NAME",
    "GATE_NODE_NAME",
    "gate_code_of",
    "render_assembly_node",
    "render_gate_node",
]

#: The node that reduces staging onto the spine and adds §10.2's system columns. One per group:
#: the three columns are added ONCE, and a second assembler would be a second place they could be.
ASSEMBLY_NODE_NAME = "assemble"
ASSEMBLY_FUNC_NAME = "assemble"

#: The node §9's shape gates live in. It writes the publication dataset — the CATALOG decides what
#: that means, and until Task 16's probe attests a mechanism the entry is fail-closed (§10.3).
GATE_NODE_NAME = "gate_and_publish"
GATE_FUNC_NAME = "gate_and_publish"

#: How several findings are joined into one message. Chosen so a reader — and a caller splitting
#: the message back apart — can tell one finding from the next: prose contains commas and
#: semicolons, and both would make the boundary a guess.
FINDING_SEPARATOR = " | "

#: Binary floating point in a published feature column. §6 resolves only ``BIGINT`` and
#: ``DECIMAL(p,s)``, so any of these in the assembled group is a value nobody governed the precision
#: of — reported under its own code rather than as a generic type mismatch, because "this column is
#: a double" and "this column is the wrong decimal" are different defects with different fixes.
FORBIDDEN_SPARK_TYPES: tuple[str, ...] = ("float", "double", "real")


def gate_code_of(message: str) -> ValidationGateCode | None:
    """The ``ValidationGateCode`` a rendered gate's message names, or ``None``.

    The rendered project cannot import ``featuregen``, so a gate's code travels as the LEADING
    token of its message. This reads it back into §14's closed vocabulary, which is what lets the
    control plane record a ``GATES_FAILED`` event carrying a code rather than a string.

    Deliberately anchored at the start: a code appearing later in a message is a code being
    *mentioned*, and treating a mention as the verdict would let any prose claim any gate.
    """
    if not isinstance(message, str):
        return None
    token = message.split(":", 1)[0].strip()
    try:
        return ValidationGateCode(token)
    except ValueError:
        return None


# ── rendering a collected finding ────────────────────────────────────────────────────────────────


def _finding(code: ValidationGateCode, text: str, *, tail: str,
             indent: str = "    ") -> list[str]:
    """``failures.append("CODE: …" + <tail>)`` — one §9 verdict, collected rather than raised.

    ``tail`` is REQUIRED and is a rendered expression: §14's rule for a gate's detail is counts,
    types, hashes, names and locations, and a finding with no tail would state that something is
    wrong without naming which column, which is the shape an operator cannot act on.
    """
    wrapped = _wrap(f"{code.value}: {text}", 84)
    inner = indent + "    "
    parts = [f'{inner}"{part} "' for part in wrapped[:-1]]
    joined = f'{inner}"{wrapped[-1]} " + {tail}'
    parts.append(joined if len(joined) + 1 <= _RENDERED_WIDTH
                 else f'{inner}"{wrapped[-1]} "\n{inner}+ {tail}')
    return [f"{indent}failures.append(", *parts[:-1], f"{parts[-1]})"]


def _raise_collected(indent: str = "    ") -> list[str]:
    """The one raise a rendered gate node performs, over everything it found."""
    return [
        *_comment(
            "One raise, over every finding. The leading token is the code of the gate that fired "
            "FIRST, which is what a caller reads back into §14's vocabulary; the rest are joined "
            "after it so an operator fixes everything this run found rather than returning here "
            "once per broken feature.", indent=indent),
        f"{indent}if failures:",
        f"{indent}    raise RuntimeError({FINDING_SEPARATOR!r}.join(failures))",
        "",
    ]


def _literal_rows(name: str, rows: list[str], *, brackets: str = "()",
                  indent: str = "    ") -> list[str]:
    """A literal, ONE element per line, always with a trailing comma.

    The trailing comma is not style: ``('x')`` is a STRING and ``('x',)`` is a one-element tuple,
    and a group with a single planned feature would otherwise render a ``planned`` that a ``name
    not in planned`` test read character by character. One element per line is what keeps a
    hundred-column artifact reviewable — a joined literal is one line nobody reads.
    """
    return [f"{indent}{name} = {brackets[0]}",
            *(f"{indent}    {row}," for row in rows),
            f"{indent}{brackets[1]}"]


# ══ §10.2 — assembly ═════════════════════════════════════════════════════════════════════════════


def render_assembly_node(
    plan: FeatureGroupPlanV1,
    *,
    spine_dataset: str,
    staging_datasets: Mapping[str, str],
    manifest_datasets: Mapping[str, str],
    assembled_dataset: str,
) -> RenderedNode:
    """Render the node that judges §9's evidence, reduces staging onto the spine, and stamps §10.2.

    The order is the spec's: *stage per feature → assemble onto the spine → add the three system
    columns once → validate*. The manifest and project-integrity gates run before the reduction
    because they decide whether this staging area is evidence about this run at all.

    Args:
        plan: the group's packing list. Its features decide the staging inputs, its ``ir_hash``es
            are what each manifest is checked against, and its key/business-date columns are what
            the reduction joins on.
        spine_dataset: the declared population (Task 13a's output) — ``(keys…, business_dt)``.
        staging_datasets: per feature column, the catalog name of its staged frame.
        manifest_datasets: per feature column, the catalog name of its ``StagingManifestV1``.
        assembled_dataset: where the assembled group is written. Declared rather than in-memory, so
            a group the shape gates then reject is still there to inspect.

    Returns:
        A :class:`~featuregen.materialize.render.project.RenderedNode` with ONE output.

    Raises:
        TypeError: ``plan`` is not a ``FeatureGroupPlanV1``.
        ValueError: a dataset name is missing or unusable, or a planned feature has no staging or
            manifest dataset — which would be a column nothing staged and nothing could prove.
    """
    if not isinstance(plan, FeatureGroupPlanV1):
        raise TypeError(
            f"rendering assembly needs the FeatureGroupPlanV1, got {type(plan).__name__}: the "
            f"published columns, the landing keys and every ir_hash §9 gates against are the "
            f"plan's, and none of them can be recovered from a dataset name")
    _dataset(spine_dataset, "the assembly node's spine dataset")
    _dataset(assembled_dataset, "the assembly node's assembled dataset")
    columns = tuple(feature.column_name for feature in plan.features)
    for column in columns:
        if column not in staging_datasets or column not in manifest_datasets:
            raise ValueError(
                f"the planned feature {column!r} has no staging or manifest dataset: §9 proves "
                f"completeness per published column, so a column with nothing to read is one the "
                f"gate could only ever report as missing")
        _dataset(staging_datasets[column], f"the staging dataset for {column!r}")
        _dataset(manifest_datasets[column], f"the manifest dataset for {column!r}")

    keys = (*plan.entity_key_columns, plan.business_dt_column)
    staged = [f"staged_{column}" for column in columns]
    documents = [f"manifest_{column}" for column in columns]

    source = "\n".join([
        *_assembly_signature(staged, documents),
        *_assembly_docstring(plan, columns),
        "    business_date = str(business_dt)",
        "    failures = []",
        "",
        *_manifest_gate_lines(plan, columns, documents),
        *_arriving_system_column_lines(columns, staged),
        *_project_integrity_lines(),
        *_raise_collected(),
        *_reduction_lines(staged, keys),
        *_system_column_lines(),
    ]) + "\n"

    return RenderedNode(
        name=ASSEMBLY_NODE_NAME,
        func_name=ASSEMBLY_FUNC_NAME,
        source=source,
        inputs=(spine_dataset,
                *(staging_datasets[column] for column in columns),
                *(manifest_datasets[column] for column in columns),
                "params:business_dt", "params:generation_id", "params:run_id",
                "params:sandbox_execution_hash"),
        outputs=(assembled_dataset,),
        imports=("from pyspark.sql import DataFrame", "from pyspark.sql import functions as F",
                 "import hashlib", "import json", "import pathlib"),
        tags=("feature_staging", "assembly"),
    )


def _assembly_signature(staged: list[str], documents: list[str]) -> list[str]:
    """One parameter per line. Kedro binds a node's inputs POSITIONALLY, and this node has as many
    inputs as the group has features twice over — a reader who miscounts reads one feature's frame
    against another feature's evidence."""
    parameters = [
        "spine: DataFrame",
        *(f"{name}: DataFrame" for name in staged),
        *(f"{name}: dict" for name in documents),
        "business_dt: str", "generation_id: str", "run_id: str", "sandbox_execution_hash: str",
    ]
    return [
        f"def {ASSEMBLY_FUNC_NAME}(",
        *(f"        {parameter}," for parameter in parameters[:-1]),
        f"        {parameters[-1]}) -> DataFrame:",
    ]


def _assembly_docstring(plan: FeatureGroupPlanV1, columns: tuple[str, ...]) -> list[str]:
    landing = ", ".join(_safe_text(name, "a landing key column")
                        for name in plan.entity_key_columns)
    return [
        '    """Assemble the group onto the spine and stamp §10.2\'s generation metadata.',
        "",
        f"    Judged first, and only then assembled: the {len(columns)} staging manifests are §9's",
        "    completeness evidence, and a group reduced from another run's staging output is not a",
        "    group anyone asked to be graded. The three system columns are added ONCE, here, after",
        "    the reduction — per-feature staging deliberately carries none of them, because one",
        "    copy per staged frame would arrive at this join as duplicates.",
        "",
        f"    The reduction is a LEFT join onto ({landing}, "
        f"{_safe_text(plan.business_dt_column, 'the business-date column')}): §8 rule 3 keeps",
        "    entities with no source rows present, and an inner join would shrink the declared",
        "    population to whichever entities happened to have data.",
        '    """',
    ]


def _manifest_gate_lines(plan: FeatureGroupPlanV1, columns: tuple[str, ...],
                         documents: list[str]) -> list[str]:
    """§9's manifest gates, in ``check_completeness``'s order and with its codes.

    The order is that function's and the reasons are its own: duplicates first (grading a name
    written twice would grade whichever copy landed last), then staleness over EVERY manifest
    present (a manifest bound to another generation/run/date is not evidence about this run, and
    judging its ``ir_hash`` would report a verdict nobody asked for), then the per-feature
    questions, then manifests naming features this plan does not contain.
    """
    hashes = [f"{feature.column_name!r}: {feature.ir_hash!r}" for feature in plan.features]
    return [
        *_comment(
            "§9 — completeness is proven by MANIFEST, not by schema: a column of the right shape "
            "cannot show which IR produced it. Every check below is over the documents the staging "
            "nodes wrote, and none of them reads a row."),
        *_literal_rows("manifests", documents),
        *_literal_rows("planned", [repr(column) for column in columns]),
        *_literal_rows("planned_ir_hashes", hashes, brackets="{}"),
        "",
        "    named = {}",
        "    for manifest in manifests:",
        "        named.setdefault(str(manifest['intent_feature_name']), []).append(manifest)",
        "",
        "    duplicated = sorted(name for name, found in named.items() if len(found) > 1)",
        "    if duplicated:",
        *_finding(
            ValidationGateCode.DUPLICATE_STAGING_MANIFEST,
            "more than one staging manifest names each of these planned features. §9 requires "
            "exactly one per feature, and choosing between them would choose which computation "
            "published:",
            tail="str(duplicated)", indent="        "),
        "",
        *_comment(
            "Staleness is judged BEFORE the ir_hash and over every manifest present: staging paths "
            "are generation-scoped, so an older SUCCESSFUL manifest whose ir_hash still matches "
            "would publish stale output past every other check."),
        "    expected_binding = (str(generation_id), str(run_id), business_date)",
        "    bindings = {name: (str(found[0]['generation_id']), str(found[0]['run_id']),",
        "                       str(found[0]['business_dt']))",
        "                for name, found in named.items()}",
        "    stale = sorted(name for name in bindings",
        "                   if name not in duplicated and bindings[name] != expected_binding)",
        "    if stale:",
        *_finding(
            ValidationGateCode.STALE_STAGING_MANIFEST,
            "these staging manifests are bound to another generation, run or business date rather "
            "than to this run's (this run, then what each of them states):",
            tail="str((expected_binding, [(name, bindings[name]) for name in stale]))",
            indent="        "),
        "",
        "    judged = [name for name in planned",
        "              if name in named and name not in duplicated and name not in stale]",
        "    missing = [name for name in planned if name not in named]",
        "    if missing:",
        *_finding(
            ValidationGateCode.MISSING_STAGING_MANIFEST,
            "these planned features have no staging manifest. A column of the right shape proves "
            "nothing about which computation produced it:",
            tail="str(missing)", indent="        "),
        "",
        "    incomplete = [name for name in judged",
        f"                  if str(named[name][0]['status']) != {StagingStatus.COMPLETED.value!r}]",
        "    if incomplete:",
        *_finding(
            ValidationGateCode.INCOMPLETE_COMPUTATION,
            "the staging manifests for these features record a status other than "
            f"{StagingStatus.COMPLETED.value!r}: the record EXISTS and says the column was never "
            "computed, which is not the same as a manifest nobody wrote:",
            tail="str([(name, str(named[name][0]['status'])) for name in incomplete])",
            indent="        "),
        "",
        "    mismatched = [name for name in judged",
        "                  if str(named[name][0]['ir_hash']) != planned_ir_hashes[name]]",
        "    if mismatched:",
        *_finding(
            ValidationGateCode.IR_HASH_MISMATCH,
            "these staging outputs were produced by an IR the plan does not name. The schema would "
            "match either way, which is exactly why the manifest carries the hash:",
            tail="str([(name, str(named[name][0]['ir_hash'])) for name in mismatched])",
            indent="        "),
        "",
        "    unplanned = sorted(name for name in named if name not in planned_ir_hashes)",
        "    if unplanned:",
        *_finding(
            ValidationGateCode.UNEXPECTED_COLUMN,
            "these staging manifests name features this group's plan does not contain, which "
            "would be an extra column in the published row:",
            tail="str(unplanned)", indent="        "),
        "",
    ]


def _arriving_system_column_lines(columns: tuple[str, ...], staged: list[str]) -> list[str]:
    """§10.2 — a system column may not ARRIVE at assembly; it is added here and only here.

    Checked rather than assumed. ``withColumn`` REPLACES a column of the same name, so a staged
    frame that already carried ``__generation_id`` would have it silently overwritten below and
    nothing would ever say that a staging node wrote a column it was told not to write.
    """
    frames = ["('the spine', spine)"] + [
        f"({column!r}, {name})" for column, name in zip(columns, staged, strict=True)]
    return [
        *_comment(
            "§10.2 — the three system columns are added ONCE, below. None of them may ARRIVE here: "
            "`withColumn` replaces a column of the same name, so one carried in from a staged "
            "frame would be overwritten without a word."),
        *_literal_rows("arriving", frames, brackets="[]"),
        *_literal_rows("system_columns", [repr(name) for name in SYSTEM_COLUMNS]),
        "    carried = [(label, name) for label, frame in arriving",
        "               for name in system_columns if name in frame.columns]",
        "    if carried:",
        *_finding(
            ValidationGateCode.UNEXPECTED_COLUMN,
            "these inputs already carry a system column §10.2 adds once at assembly:",
            tail="str(carried)", indent="        "),
        "",
    ]


def _project_integrity_lines() -> list[str]:
    """§9's ``PROJECT_INTEGRITY`` — the lock is checked against the bytes that are actually here.

    L0 (§11.2) asks the same question at run preparation, on the submitting machine. This asks it
    on the cluster, about the copy that arrived: the two are the same question in the two places a
    project can differ, and only this one can see a project edited after it was validated.

    The recomputation mirrors :func:`~featuregen.materialize.identity.generated_project_hash`:
    ``sha256`` per file under its project-relative POSIX path, then ``sha256`` of the canonical JSON
    of ``{"files": …}``. Canonical JSON is RFC 8785 there and ``sort_keys`` + tight separators here;
    the two agree because every key is an ASCII path and every value an ASCII hex digest, which is
    what a test pins by running this against a really sealed project rather than by asserting it.

    Three kinds of file are skipped, each because it is created by RUNNING the project rather than
    by rendering it: Python's own byte cache, the ``*.egg-info`` an editable install writes inside
    the project, and dot-prefixed tooling directories. Anything else present and unsealed FAILS —
    an added module can shadow a rendered one, so it is a different project.
    """
    return [
        *_comment(
            "§9 PROJECT_INTEGRITY. The lock states a hash over the OTHER files; this recomputes it "
            "over the bytes that actually arrived. L0 asks the same question at run preparation, "
            "on the submitting machine — this one is the only one that can see a project edited "
            "after it was validated."),
        f"    project_root = pathlib.Path(__file__).resolve().parents[{_LOCK_DEPTH}]",
        f"    lock = json.loads("
        f"(project_root / {GENERATED_LOCK_FILENAME!r}).read_text(encoding='utf-8'))",
        "",
        *_comment(
            "Skipped: Python's byte cache, the `*.egg-info` an editable install writes INSIDE the "
            "project, and dot-prefixed tooling directories. All three are created by RUNNING the "
            "project rather than by rendering it, and a gate that fired on them would refuse every "
            "run that had ever been imported. Anything else unsealed fails — an added module can "
            "shadow a rendered one."),
        "    digests = {}",
        "    for path in project_root.rglob('*'):",
        "        if not path.is_file():",
        "            continue",
        "        parts = path.relative_to(project_root).parts",
        "        if any(part == '__pycache__' or part.startswith('.') or part.endswith('.egg-info')",
        "               for part in parts):",
        "            continue",
        "        relative = '/'.join(parts)",
        f"        if relative == {GENERATED_LOCK_FILENAME!r} or relative.endswith('.pyc'):",
        "            continue",
        "        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()",
        "    observed_project_hash = hashlib.sha256(json.dumps(",
        "        {'files': digests}, separators=(',', ':'), sort_keys=True,",
        "        ensure_ascii=False).encode('utf-8')).hexdigest()",
        "    if observed_project_hash != str(lock['generated_project_hash']):",
        *_finding(
            ValidationGateCode.PROJECT_INTEGRITY,
            "the project on this cluster does not hash to what GENERATED.lock records, so the "
            "artifact that is running is not the artifact that was sealed and validated. Files "
            "hashed, observed, recorded:",
            tail=("str((len(digests), observed_project_hash, "
                  "str(lock['generated_project_hash'])))"),
            indent="        "),
        "",
    ]


def _reduction_lines(staged: list[str], keys: tuple[str, ...]) -> list[str]:
    """§8 rule 3 — one LEFT join per feature, in the plan's order.

    The order is the plan's so the assembled frame arrives in ``expected_schema``'s order without
    a repairing ``select``: a select that put the columns right would also hide a staged frame that
    carried a column nobody planned, which is a finding rather than something to tidy away.
    """
    key_literal = ", ".join(repr(name) for name in keys)
    lines = [
        *_comment(
            "§8 rule 3 — LEFT, so an entity the declared population contains but no source row "
            "mentions is still in the published group. An inner join would shrink the population "
            "to whichever entities happened to have data, and every count would move with it. No "
            "`select` follows: putting the columns right here would also hide a staged frame that "
            "carried one nobody planned, and that is §9's finding to make."),
        "    assembled = spine",
    ]
    for name in staged:
        lines.extend(_call_lines(
            "    assembled = assembled.join(",
            [name, f"on=[{key_literal}]", "how='left'"], ")"))
    lines.append("")
    return lines


def _system_column_lines() -> list[str]:
    """§10.2 — the three, added once, from the three places they may honestly come from."""
    return [
        *_comment(
            "§10.2 — the published generation metadata, added ONCE and only here. Two of the "
            "three cannot be literals: the project hash would be self-referential (§7 keeps it out "
            "of every generated file but the lock, because the hash is taken OVER those files), "
            "and the execution hash is a run-time value that arrives as a prepared run parameter "
            "(§11.1). They live in the DATA because §10.3's atomicity probe needs a generation "
            "marker visible in the same atomic state as the rows."),
        "    assembled = assembled.withColumn(",
        f"        {SYSTEM_COLUMNS[0]!r}, F.lit(str(generation_id)))",
        "    assembled = assembled.withColumn(",
        f"        {SYSTEM_COLUMNS[1]!r}, F.lit(str(lock['generated_project_hash'])))",
        "    assembled = assembled.withColumn(",
        f"        {SYSTEM_COLUMNS[2]!r}, F.lit(str(sandbox_execution_hash)))",
        "    return assembled",
    ]


# ══ §9 — the shape gates ═════════════════════════════════════════════════════════════════════════


def render_gate_node(
    plan: FeatureGroupPlanV1,
    *,
    assembled_dataset: str,
    published_dataset: str,
) -> RenderedNode:
    """Render §9's blocking shape gates over the assembled group.

    Six checks, in the order the questions depend on each other: which columns are present (nothing
    else can be judged until that is settled, so it raises on its own), then forbidden numerics,
    declared types, nullability on real rows, key uniqueness, and finally the schema hash.

    The node returns the frame it validated. It contains no publication mechanism, no table name
    and no DDL — the catalog entry decides where the frame goes, and §10.3 forbids selecting a
    mechanism before Task 16's probe attests one, which is why that entry is fail-closed today.

    Args:
        plan: the packing list ``expected_schema`` and ``expected_schema_hash`` are read from.
        assembled_dataset: the assembled group, written by :func:`render_assembly_node`.
        published_dataset: the publication target's catalog entry.

    Raises:
        TypeError: ``plan`` is not a ``FeatureGroupPlanV1``.
        ValueError: a dataset name is missing or unusable.
    """
    if not isinstance(plan, FeatureGroupPlanV1):
        raise TypeError(
            f"rendering §9's gates needs the FeatureGroupPlanV1, got {type(plan).__name__}: the "
            f"expected schema and its hash are the plan's, and a gate that inferred them from the "
            f"frame it is judging would agree with whatever it was given")
    _dataset(assembled_dataset, "the gate node's assembled dataset")
    _dataset(published_dataset, "the gate node's published dataset")

    schema = expected_schema(plan)
    rows = [
        f"({column.name!r}, {column.role.value!r}, {column.sql_type!r}, {column.nullable!r})"
        for column in schema
    ]
    keys = (*plan.entity_key_columns, plan.business_dt_column)

    source = "\n".join([
        f"def {GATE_FUNC_NAME}(assembled: DataFrame) -> DataFrame:",
        *_gate_docstring(plan),
        *_comment(
            "§9's expected published row (§10.2 included): name, why the column is there, the "
            "type §6 decided where anything decided one, and whether a NULL may appear. The "
            "landing key and the business date carry NO type — nothing governed states one at "
            "compile time, and a gate enforcing an invented one would refuse a correct table."),
        *_literal_rows("expected", rows),
        "    expected_names = [name for name, _role, _type, _nullable in expected]",
        "    failures = []",
        "",
        *_presence_lines(),
        *_forbidden_numeric_lines(),
        *_declared_type_lines(),
        *_nullability_lines(),
        *_key_uniqueness_lines(keys),
        *_schema_hash_lines(plan),
        *_raise_collected(),
        "    return assembled",
    ]) + "\n"

    return RenderedNode(
        name=GATE_NODE_NAME,
        func_name=GATE_FUNC_NAME,
        source=source,
        inputs=(assembled_dataset,),
        outputs=(published_dataset,),
        imports=("from pyspark.sql import DataFrame", "from pyspark.sql import functions as F",
                 "import hashlib", "import json"),
        tags=("feature", "gate"),
    )


def _gate_docstring(plan: FeatureGroupPlanV1) -> list[str]:
    return [
        '    """§9\'s blocking gates. Any failure rejects the group; the previous partition stays.',
        "",
        "    Every check here is a failure on REAL ROWS — that is what makes them gates rather than",
        "    compilation refusals. The frame returned is the frame that passed: the catalog entry",
        "    decides what publishing it means, and no mechanism may be selected before §10.3's",
        "    probe attests one.",
        '    """',
    ]


def _presence_lines() -> list[str]:
    """Presence is judged FIRST and ALONE: every other check reads a column by name."""
    return [
        *_comment(
            "Required columns present, none extra (§9). Judged first and on its own — every check "
            "below reads a column BY NAME, and running them against a frame whose columns are "
            "already wrong would report consequences instead of the cause."),
        "    observed = dict(assembled.dtypes)",
        "    absent = [name for name in expected_names if name not in observed]",
        "    if absent:",
        *_finding(
            ValidationGateCode.MISSING_FEATURE_COLUMN,
            "the assembled group is missing columns the plan requires. The published row is the "
            "plan's, and a shorter one is a different contract:",
            tail="str(absent)", indent="        "),
        "    extra = [name for name in observed if name not in expected_names]",
        "    if extra:",
        *_finding(
            ValidationGateCode.UNEXPECTED_COLUMN,
            "the assembled group carries columns the plan does not: a column nobody planned is a "
            "column nobody governed the sensitivity, retention or type of:",
            tail="str(extra)", indent="        "),
        "    if failures:",
        f"        raise RuntimeError({FINDING_SEPARATOR!r}.join(failures))",
        "",
        *_comment(
            "Types are compared with case and spacing normalized: Spark answers `decimal(18,2)` "
            "and §6 decided `DECIMAL(18,2)`, and a comparison that called those different would "
            "fire on every correct run."),
        "    normalized = {name: dtype.replace(' ', '').upper()",
        "                  for name, dtype in observed.items()}",
        "",
    ]


def _forbidden_numeric_lines() -> list[str]:
    """Before the type check, so a double is reported as what it is."""
    forbidden = ", ".join(repr(name) for name in FORBIDDEN_SPARK_TYPES)  # a tuple, always
    return [
        *_comment(
            "§9 — forbidden numerics. §6 resolves only BIGINT and DECIMAL(p,s), so binary floating "
            "point in a published column is a value nobody governed the precision of. Reported "
            "before the type check so it arrives under its own name: 'this column is a double' and "
            "'this column is the wrong decimal' have different fixes."),
        f"    forbidden_types = ({forbidden},)",
        "    forbidden = sorted(name for name, dtype in observed.items()",
        "                       if dtype.strip().lower() in forbidden_types)",
        "    if forbidden:",
        *_finding(
            ValidationGateCode.FORBIDDEN_NUMERIC,
            "these columns hold binary floating point, which §6 never resolves for a published "
            "feature. A float column is a number nobody governed the precision of:",
            tail="str([(name, observed[name]) for name in forbidden])", indent="        "),
        "",
    ]


def _declared_type_lines() -> list[str]:
    return [
        *_comment(
            "§9 — physical types match §6. Only where §6 DECIDED one: the landing key and the "
            "business date carry no compile-time type, and checking them against a guess would "
            "refuse a correct table."),
        "    mistyped = [(name, observed[name], declared)",
        "                for name, _role, declared, _nullable in expected",
        "                if declared is not None and name not in forbidden",
        "                and normalized[name] != declared.replace(' ', '').upper()]",
        "    if mistyped:",
        *_finding(
            ValidationGateCode.WRONG_COLUMN_TYPE,
            "these columns are not the physical type §6 resolved for them (column, observed, "
            "declared):",
            tail="str(mistyped)", indent="        "),
        "",
    ]


def _nullability_lines() -> list[str]:
    """Nullability is judged on ROWS, not on the schema flag — see the rendered comment."""
    return [
        *_comment(
            "§9 — nullability. Judged on the ROWS, not on the frame's nullable FLAG: Spark widens "
            "that flag through a left join and through most reads, so a schema-flag comparison "
            "would refuse every correct run of a group that has any non-nullable column. What the "
            "declaration actually forbids is a NULL reaching the published table, and that is "
            "what is counted. `limit(1).count()` reads no value out of the cluster."),
        "    nulled = [name for name, _role, _declared, nullable in expected if not nullable",
        "              and assembled.where(F.col(name).isNull()).limit(1).count() > 0]",
        "    if nulled:",
        *_finding(
            ValidationGateCode.WRONG_NULLABILITY,
            "these columns are declared NOT NULL and hold at least one NULL. A NULL landing key "
            "is not a landing key, and a NULL generation marker defeats §10.2:",
            tail="str(nulled)", indent="        "),
        "",
    ]


def _key_uniqueness_lines(keys: tuple[str, ...]) -> list[str]:
    landing = ", ".join(_safe_text(name, "a landing key column") for name in keys)
    return [
        *_comment(
            f"§9 — key uniqueness. Exactly one row per ({landing}). A duplicate is a blocking gate "
            f"rather than something to de-duplicate: collapsing the rows would hide whichever join "
            f"fanned out, and every value downstream would move with it."),
        *_call_lines("    duplicated = assembled.groupBy(",
                     [f"F.col({name!r})" for name in keys], ").count().where(F.col('count') > 1)"),
        "    if duplicated.limit(1).count() > 0:",
        *_finding(
            ValidationGateCode.KEY_NOT_UNIQUE,
            "the assembled group holds more than one row for a landing key. Number of keys "
            "affected:",
            tail="str(duplicated.count())", indent="        "),
        "",
    ]


def _schema_hash_lines(plan: FeatureGroupPlanV1) -> list[str]:
    """The assembled schema hash, over the OBSERVED order.

    Not redundant with the checks above, and the reason is the order: once presence, types and
    nullability agree, the one thing left that this can see is a published row whose columns arrive
    in a different sequence — which changes nothing a name-keyed check can detect and changes the
    bytes of every downstream reader that reads positionally.
    """
    return [
        *_comment(
            "§9 — the assembled schema hash, over the columns IN THE ORDER they arrive. The checks "
            "above match names to types, so what this adds is order: a published row whose columns "
            "are permuted passes every one of them and is still not the row the plan describes. "
            "The payload is `expected_schema`'s, so the two hashes are comparable by construction."),
        "    payload = []",
        "    by_name = {name: (role, declared, nullable)",
        "               for name, role, declared, nullable in expected}",
        "    for name in assembled.columns:",
        "        role, declared, nullable = by_name[name]",
        "        payload.append({'name': name, 'role': role, 'nullable': nullable,",
        "                        'sql_type': None if declared is None else normalized[name]})",
        "    schema_hash = hashlib.sha256(json.dumps(",
        "        {'columns': payload}, separators=(',', ':'), sort_keys=True,",
        "        ensure_ascii=False).encode('utf-8')).hexdigest()",
        f"    if schema_hash != {expected_schema_hash(plan)!r}:",
        *_finding(
            ValidationGateCode.SCHEMA_HASH_MISMATCH,
            "the assembled group's schema hash is not the plan's. Observed:",
            tail="schema_hash", indent="        "),
        "",
    ]


#: Kept so a reader of this module can see that ``ColumnRole`` is what the rendered literal carries
#: — the roles are §9's own reason for gating three classes of column differently.
_ROLES: tuple[str, ...] = tuple(role.value for role in ColumnRole)
