"""Data-agent control-plane package.

Canonicalization, contracts and refusal codes for governed data access, observation and analysis.

**No runtime data-plane dependencies live here.** No PySpark, no Hive client, no Kedro. This package
decides *what* may be read and *how the result is shaped*; an executor decides how to run it, and
executors are pluggable (roadmap §3c: direct Hive SQL first, ODS native SQL later, Spark/Kedro for
scale and production). Keeping the boundary here is what lets the ontology consume identical typed
evidence without knowing which executor produced it.
"""
