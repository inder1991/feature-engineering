"""The SPARK-SEMANTICS GATE — the aggregate-overflow refusal, proven on the real engine.

**Not named ``test_*`` on purpose, so the main suite never collects it** — the same split as
``l0_gate.py``, and for the same reason: ``pyspark`` is not a dependency of this platform, and a
suite that pulled it in would trade ~4s for a JVM-capable interpreter. Run it explicitly::

    FEATUREGEN_L0_PYTHON=$PWD/.venv-artifact/bin/python \\
    PYTHONPATH=$PWD/src .venv/bin/python -m pytest \\
        tests/featuregen/materialize/spark_semantics_gate.py -q

**What the main suite proves and what only this can.** ``fake_spark`` computes exactly —
arbitrary-precision decimals under a widened context — so neither a sum nor a subtraction can
EVER overflow there, and neither the aggregate-level nor the operation-level OVERFLOW_VIOLATION
gate can fire. The suite therefore proves the gates do NOT fire where they must not (an all-null
`ignore` group, an empty window, a `null` zero_denominator). That they FIRE on a genuine
overflow is a claim about Spark itself: with ANSI off (the setting the rendered ``spark.yml``
pins), Spark answers a decimal result exceeding its own result type with NULL, in two places the
publish-cast check structurally cannot see. FIRST, inside the aggregation
(``CheckOverflowInSum``): two rows of ``Decimal("9.99e+35")`` in a ``DECIMAL(38,2)`` operand sum
to 1.998e+36 — one integer digit more than DECIMAL(38,2) holds. SECOND, the final operation's
own arithmetic: a minuend of 9.99e+35 and a subtrahend of -9.99e+35 each fit DECIMAL(38,2) and
each per-operand sum is healthy, yet the Subtract's own DECIMAL(38,2) result type cannot hold
their 1.998e+36 difference. Both are the measured repros this file replays through the RENDERED
node sources on the real engine.

The environment contract is ``l0_gate.py``'s: ``FEATUREGEN_L0_PYTHON`` names the interpreter that
has pyspark (skipped, never faked, when absent), and ``PYSPARK_PYTHON``/``PYSPARK_DRIVER_PYTHON``
are both exported so Spark's workers do not land on the system Python.
"""
from __future__ import annotations

import json
import os
import subprocess

from tests.featuregen.materialize.l0_gate import l0_env, l0_python  # noqa: F401 — fixtures
from tests.featuregen.materialize.test_group_plan import (  # noqa: F401 — `catalog` is a fixture
    BUSINESS_DT,
    catalog,
)
from tests.featuregen.materialize.test_render_nodes_compute import (  # noqa: F401 — fixtures
    _calculate,
    _render_difference,
    compiled,
    feature,
    ratio,
    ratio_feature,
)

#: The measured repro: 9.99e+35 has 36 integer digits — the most DECIMAL(38,2) holds — so ONE row
#: fits and the two-row sum (1.998e+36) exceeds the sum's own DECIMAL(38,2) result type.
OVERFLOWING = '"9.99e+35"'

#: The driver run under the L0 interpreter. `.format` placeholders only — no braces elsewhere.
_DRIVER = '''\
"""Replays the rendered calculation node against REAL Spark — see spark_semantics_gate.py."""
import datetime as dt
import importlib.util
import sys
from decimal import Decimal

from pyspark.sql import SparkSession
from pyspark.sql import types as T

# The two settings the rendered spark.yml pins (Task 2): the governed gates are written against
# ANSI-OFF semantics — under ANSI the overflow raises a raw SparkArithmeticException instead of
# yielding the NULL the gate reads — and a session zone inherited from the JVM is a second clock.
spark = (SparkSession.builder.master("local[1]").appName("spark-semantics-gate")
         .config("spark.sql.ansi.enabled", "false")
         .config("spark.sql.session.timeZone", "UTC")
         .config("spark.ui.enabled", "false")
         .getOrCreate())
spark.sparkContext.setLogLevel("ERROR")

spec = importlib.util.spec_from_file_location("gen_nodes", {nodes_path!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
calculate = getattr(module, {func_name!r})

SCHEMA = T.StructType([
    T.StructField("cif_id", T.StringType()),
    T.StructField("dr_cr_flag", T.StringType()),
    T.StructField("status_cd", T.StringType()),
    T.StructField("txn_amt", T.DecimalType(38, 2)),
])
SPINE = spark.createDataFrame(
    [("c1", dt.date.fromisoformat({business_dt!r}))],
    T.StructType([
        T.StructField("cif_id", T.StringType()),
        T.StructField("business_dt", T.DateType()),
    ]))


def run(amount):
    projection = spark.createDataFrame(
        [("c1", "D", "posted", Decimal(amount)), ("c1", "D", "posted", Decimal(amount))], SCHEMA)
    return calculate(projection, SPINE, {business_dt!r}, "gen-gate", "run-gate", "exec-gate",
                     "hdfs://nn/staging/gen-gate")


# The CONTROL first: two values that fit must publish, or a gate that always fired would also
# "pass" the overflow half below.
staged, manifest = run("100.50")
if manifest["row_count"] != 1:
    print("CONTROL FAILED: expected 1 staged row, got", manifest["row_count"])
    sys.exit(2)

try:
    staged, manifest = run({overflowing})
except RuntimeError as refused:
    if "OVERFLOW_VIOLATION" in str(refused):
        print("RAISED OVERFLOW_VIOLATION:", refused)
        sys.exit(0)
    print("RAISED THE WRONG REFUSAL:", refused)
    sys.exit(3)
print("PUBLISHED WITHOUT RAISING - the overflow NULL went through:",
      [row.asDict() for row in staged.collect()])
sys.exit(4)
'''


#: The DIFFERENCE driver — the operation-level repro. Each operand fits DECIMAL(38,2) on its own
#: and each per-operand sum is healthy; only the Subtract's own result type overflows.
_DIFFERENCE_DRIVER = '''\
"""Replays the rendered DIFFERENCE node against REAL Spark — see spark_semantics_gate.py."""
import datetime as dt
import importlib.util
import sys
from decimal import Decimal

from pyspark.sql import SparkSession
from pyspark.sql import types as T

# The two settings the rendered spark.yml pins (Task 2) — see the identity driver above.
spark = (SparkSession.builder.master("local[1]").appName("spark-semantics-gate")
         .config("spark.sql.ansi.enabled", "false")
         .config("spark.sql.session.timeZone", "UTC")
         .config("spark.ui.enabled", "false")
         .getOrCreate())
spark.sparkContext.setLogLevel("ERROR")

spec = importlib.util.spec_from_file_location("gen_nodes", {nodes_path!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
calculate = getattr(module, {func_name!r})

SCHEMA = T.StructType([
    T.StructField("cif_id", T.StringType()),
    T.StructField("cross_border_flag", T.BooleanType()),
    T.StructField("txn_amt", T.DecimalType(38, 2)),
])
SPINE = spark.createDataFrame(
    [("c1", dt.date.fromisoformat({business_dt!r}))],
    T.StructType([
        T.StructField("cif_id", T.StringType()),
        T.StructField("business_dt", T.DateType()),
    ]))


def run(minuend, subtrahend):
    minuend_frame = spark.createDataFrame([("c1", True, Decimal(minuend))], SCHEMA)
    subtrahend_frame = spark.createDataFrame([("c1", True, Decimal(subtrahend))], SCHEMA)
    return calculate(minuend_frame, subtrahend_frame, SPINE, {business_dt!r}, "gen-gate",
                     "run-gate", "exec-gate", "hdfs://nn/staging/gen-gate")


# The CONTROL first: a difference that fits must publish, or a gate that always fired would also
# "pass" the overflow half below.
staged, manifest = run("100.50", "40.25")
if manifest["row_count"] != 1:
    print("CONTROL FAILED: expected 1 staged row, got", manifest["row_count"])
    sys.exit(2)

# 9.99e+35 and -9.99e+35 each fit DECIMAL(38,2) and BOTH per-operand sums are healthy — the
# overflow happens in the SUBTRACTION, whose own DECIMAL(38,2) result type cannot hold 1.998e+36.
try:
    staged, manifest = run({overflowing}, "-" + {overflowing})
except RuntimeError as refused:
    if "OVERFLOW_VIOLATION" in str(refused):
        print("RAISED OVERFLOW_VIOLATION:", refused)
        sys.exit(0)
    print("RAISED THE WRONG REFUSAL:", refused)
    sys.exit(3)
print("PUBLISHED WITHOUT RAISING - the overflow NULL went through:",
      [row.asDict() for row in staged.collect()])
sys.exit(4)
'''


def _prove(node, driver_source: str, tmp_path, l0_python: str, l0_env) -> None:  # noqa: F811
    """Write the rendered node at lock depth, drive it under the L0 interpreter, assert the raise.

    The node is written where a generated ``nodes.py`` lives — ``GENERATED.lock`` is resolved
    ``parents[4]`` up from ``__file__`` — so the run reads the lock exactly as a deployed
    project would. The driver's exit code IS the verdict: 0 only on the OVERFLOW_VIOLATION path.
    """
    nodes_py = tmp_path / "src" / "pkg" / "pipelines" / "materialize" / "nodes.py"
    nodes_py.parent.mkdir(parents=True)
    nodes_py.write_text("\n".join(node.imports) + "\n\n\n" + node.source, encoding="utf-8")
    (tmp_path / "GENERATED.lock").write_text(
        json.dumps({"compilation": {}, "generated_project_hash": "spark-semantics-gate"}),
        encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(driver_source.format(nodes_path=str(nodes_py), func_name=node.func_name,
                                           business_dt=BUSINESS_DT, overflowing=OVERFLOWING),
                      encoding="utf-8")

    proved = subprocess.run(  # noqa: S603
        [l0_python, str(driver)], capture_output=True, text=True, check=False, timeout=480,
        env={**os.environ, **l0_env})
    assert proved.returncode == 0, \
        f"exit {proved.returncode}\nstdout:\n{proved.stdout}\nstderr:\n{proved.stderr}"
    assert "RAISED OVERFLOW_VIOLATION" in proved.stdout


def test_a_sum_that_overflows_INSIDE_the_aggregation_raises_OVERFLOW_VIOLATION(
        compiled, feature, tmp_path, l0_python, l0_env) -> None:  # noqa: F811 — pytest fixtures
    """The aggregate-level claim `fake_spark` structurally cannot make, on the engine that
    decides it: a run whose SUM overflows its own result type must REFUSE, not publish a NULL."""
    _prove(_calculate(compiled, feature), _DRIVER, tmp_path, l0_python, l0_env)


def test_a_final_operation_that_overflows_ITS_OWN_result_type_raises_OVERFLOW_VIOLATION(
        ratio, ratio_feature, tmp_path, l0_python, l0_env) -> None:  # noqa: F811 — pytest fixtures
    """The operation-level claim: both operands present and healthy, and the SUBTRACTION's own
    result type overflowed — the NULL neither the per-operand gates nor the cast check can see."""
    _prove(_render_difference(ratio, ratio_feature), _DIFFERENCE_DRIVER, tmp_path, l0_python,
           l0_env)
