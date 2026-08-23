"""§12 — the deterministic certification programme: contract, comparisons, runner, issuer.

The owner's ruling, applied verbatim: a case passes only when the NORMALIZED SEMANTIC IR exactly
matches the expert-approved IR **and** executing it against reviewed test data produces the
expected rows — failure of either fails the case, and no comparison ever looks at rendered
source bytes (a renderer whitespace change must never look like a compiler defect).

Three honesty rules this module enforces mechanically:

* **Zero provider dispatches is an assertion.** An attempt that recorded one fails regardless of
  its comparisons — the programme's whole claim is that the deterministic lane spends nothing.
* **No execution result is `UNMEASURED`** — not a pass, not a failure, and it cannot certify.
  A deployment with no execution seam CANNOT certify (step 0b's posture).
* **An empty corpus certifies NOTHING.** All-cases-passed over zero cases is vacuous truth, and
  a certificate minted from it would be the strongest claim on the weakest evidence.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from featuregen.canonical import jcs_sha256

__all__ = [
    "COMPILER_PROGRAMME_VERSION",
    "CompilerEvaluationContractV1",
    "compare_expected_rows",
    "compare_ir_payloads",
    "current_compiler_contract",
    "issue_compiler_certificate",
    "record_compiler_case",
    "run_compiler_case",
]

#: Both 1 in this generation — matching every other evaluator constant in the platform (measured:
#: all evaluator version constants are 1 in both generations).
COMPILER_PROGRAMME_VERSION = 1
DETERMINISTIC_PRODUCER_VERSION = 1


@dataclass(frozen=True, slots=True)
class CompilerEvaluationContractV1:
    """The compiler programme's world — NO provider hashes, structurally (§12 piece 1: 1097's
    contract requires them NOT NULL, and a compiler run has neither; fabricating them would make
    two different programmes indistinguishable at the one place they must differ)."""

    compiler_programme_version: int
    grammar_version: int
    producer_version: int
    canonicalization_version: int
    corpus_version: int
    corpus_content_hash: str
    expectation_registry_hash: str

    @property
    def contract_hash(self) -> str:
        return jcs_sha256(asdict(self))


def current_compiler_contract(conn, *, corpus_version: int = 1) -> CompilerEvaluationContractV1:
    """Derive the contract from THIS build + the recorded corpus, and persist it idempotently."""
    from featuregen.formula.schema_v3 import (
        CANONICALIZATION_VERSION_V3,
        OPERATION_GRAMMAR_VERSION_V3,
    )
    from featuregen.overlay.upload.recipe_formula_evaluation_contract import (
        v2_expectation_registry_hash,
    )

    cases = conn.execute(
        "SELECT case_revision_hash FROM recipe_compiler_eval_case "
        "ORDER BY case_revision_hash").fetchall()
    contract = CompilerEvaluationContractV1(
        compiler_programme_version=COMPILER_PROGRAMME_VERSION,
        grammar_version=OPERATION_GRAMMAR_VERSION_V3,
        producer_version=DETERMINISTIC_PRODUCER_VERSION,
        canonicalization_version=CANONICALIZATION_VERSION_V3,
        corpus_version=corpus_version,
        corpus_content_hash=jcs_sha256({"cases": [c[0] for c in cases]}),
        expectation_registry_hash=v2_expectation_registry_hash())
    conn.execute(
        "INSERT INTO recipe_compiler_evaluation_contract (contract_hash, "
        "compiler_programme_version, grammar_version, producer_version, "
        "canonicalization_version, corpus_version, corpus_content_hash, "
        "expectation_registry_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (contract_hash) DO NOTHING",
        (contract.contract_hash, contract.compiler_programme_version,
         contract.grammar_version, contract.producer_version,
         contract.canonicalization_version, contract.corpus_version,
         contract.corpus_content_hash, contract.expectation_registry_hash))
    return contract


def record_compiler_case(
    conn, *, expectation_ref: str, blueprint_revision: str, blueprint_hash: str,
    approved_ir: Mapping[str, Any], dataset_pin: str,
    expected_rows: Sequence[Mapping[str, Any]], runtime_profile: Mapping[str, Any],
    approved_by: Sequence[str],
) -> str:
    """ONE governed case revision (§12.2): every part under one immutable hash reviewers approve
    together. Idempotent on that hash."""
    case_revision_hash = jcs_sha256({
        "expectation_ref": expectation_ref,
        "blueprint_revision": blueprint_revision,
        "blueprint_hash": blueprint_hash,
        "approved_ir": dict(approved_ir),
        "dataset_pin": dataset_pin,
        "expected_rows": [dict(r) for r in expected_rows],
        "declared_tolerances": [],
        "runtime_profile": dict(runtime_profile),
    })
    conn.execute(
        "INSERT INTO recipe_compiler_eval_case (case_revision_hash, expectation_ref, "
        "blueprint_revision, blueprint_hash, approved_ir_json, approved_ir_hash, dataset_pin, "
        "expected_rows_json, expected_rows_hash, runtime_profile_json, approved_by) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb) "
        "ON CONFLICT (case_revision_hash) DO NOTHING",
        (case_revision_hash, expectation_ref, blueprint_revision, blueprint_hash,
         json.dumps(dict(approved_ir)), jcs_sha256(dict(approved_ir)), dataset_pin,
         json.dumps([dict(r) for r in expected_rows]),
         jcs_sha256({"rows": [dict(r) for r in expected_rows]}),
         json.dumps(dict(runtime_profile)), json.dumps(list(approved_by))))
    return case_revision_hash


# ── Comparison A — the normalized semantic IR, structurally ─────────────────────────────────────
def compare_ir_payloads(
    approved: Mapping[str, Any], produced: Mapping[str, Any],
) -> tuple[str, str | None]:
    """Field-by-field over the identity payloads, reporting the FIRST differing path.

    Returns ``(verdict, first_difference_path)`` with verdict in
    ``MATCHED | DIFFERED | INPUT_IDENTITY_MOVED``. The hash is the SUMMARY, never the evidence —
    a mismatch names where, because "where" is the entire value of this comparison. When every
    difference sits under ``formula_content_hash`` the verdict is ``INPUT_IDENTITY_MOVED``
    (§12.1's subtlety): the producer's serialization moved without a semantic change, the case
    still FAILS (the ruling admits no third verdict), and the distinct verdict buys the correct
    remedy — re-approve the fixture, a governance act, not a compiler debug.
    """
    differences: list[str] = []

    def _walk(path: str, a: Any, b: Any) -> None:
        if differences and len(differences) > 32:
            return
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            for key in sorted(set(a) | set(b)):
                if key not in a or key not in b:
                    differences.append(f"{path}.{key}" if path else key)
                else:
                    _walk(f"{path}.{key}" if path else key, a[key], b[key])
        elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if len(a) != len(b):
                differences.append(f"{path}.length")
                return
            for index, (item_a, item_b) in enumerate(zip(a, b)):
                _walk(f"{path}[{index}]", item_a, item_b)
        elif a != b:
            differences.append(path)

    _walk("", dict(approved), dict(produced))
    if not differences:
        return "MATCHED", None
    if all("formula_content_hash" in d for d in differences):
        return "INPUT_IDENTITY_MOVED", differences[0]
    return "DIFFERED", differences[0]


# ── Comparison B — executed values against reviewed test data ───────────────────────────────────
_ROUNDING = {"HALF_UP": ROUND_HALF_UP, "HALF_EVEN": ROUND_HALF_EVEN}


def compare_expected_rows(
    expected: Sequence[Mapping[str, Any]], actual: Sequence[Mapping[str, Any]], *,
    grain_keys: Sequence[str], decimal_policies: Mapping[str, Any] | None = None,
) -> tuple[str, str | None]:
    """The banking rules, exactly (§12.1): row count exact in BOTH directions; grain keys as a
    MULTISET (a duplicated grain row is a failure, not an extra); ``NULL`` / ``0`` / row-absent
    are THREE different answers; decimals exact AFTER the DECLARED policy's quantize — the policy
    comes from the formula, never from this comparator's own choice. No tolerances: the grammar
    cannot express an approximate operation (R16)."""
    if len(expected) != len(actual):
        return "DIFFERED", f"row_count expected {len(expected)} actual {len(actual)}"

    def _grain(row: Mapping[str, Any]) -> tuple:
        return tuple(row.get(k) for k in grain_keys)

    expected_by_grain: dict[tuple, list[Mapping[str, Any]]] = {}
    for row in expected:
        expected_by_grain.setdefault(_grain(row), []).append(row)
    for row in actual:
        bucket = expected_by_grain.get(_grain(row))
        if not bucket:
            return "DIFFERED", f"grain {_grain(row)!r} unexpected or duplicated"
        expected_row = bucket.pop(0)
        for column in sorted(set(expected_row) | set(row)):
            in_expected, in_actual = column in expected_row, column in row
            if in_expected != in_actual:
                # Row-absent vs present — the third answer, distinct from NULL and from 0.
                return "DIFFERED", f"grain {_grain(row)!r} column {column!r} presence"
            want, got = expected_row[column], row[column]
            if want is None or got is None:
                if want is not got:
                    return "DIFFERED", f"grain {_grain(row)!r} column {column!r} null-vs-value"
                continue
            policy = (decimal_policies or {}).get(column)
            if policy is not None:
                mode = _ROUNDING.get(getattr(policy.rounding, "name", str(policy.rounding)))
                if mode is None:
                    # The renderer implements exactly two modes; the others refuse at render, so
                    # a case declaring one can never legally reach this comparator (§12.1).
                    return "DIFFERED", (f"column {column!r} declares rounding "
                                        f"{policy.rounding!r} the renderer cannot produce")
                quantum = Decimal(1).scaleb(-policy.scale)
                want = Decimal(str(want)).quantize(quantum, rounding=mode)
                got = Decimal(str(got))
            if want != got:
                return "DIFFERED", f"grain {_grain(row)!r} column {column!r} value"
    leftover = [g for g, rows in expected_by_grain.items() if rows]
    if leftover:
        return "DIFFERED", f"grain {leftover[0]!r} expected and absent"
    return "MATCHED", None


# ── the runner ──────────────────────────────────────────────────────────────────────────────────
def run_compiler_case(
    conn, *, attempt_id: str, case_revision_hash: str, contract_hash: str,
    produced_ir: Mapping[str, Any], executed_rows: Sequence[Mapping[str, Any]] | None,
    grain_keys: Sequence[str], provider_dispatch_count: int = 0,
    decimal_policies: Mapping[str, Any] | None = None,
) -> str:
    """Evaluate ONE case and record the attempt. Returns the outcome."""
    case = conn.execute(
        "SELECT approved_ir_json, expected_rows_json FROM recipe_compiler_eval_case "
        "WHERE case_revision_hash = %s", (case_revision_hash,)).fetchone()
    if case is None:
        raise ValueError(f"no compiler case {case_revision_hash!r}: an attempt evaluates an "
                         f"approved case, and there is nothing approved here")
    approved_ir = case[0] if isinstance(case[0], dict) else json.loads(case[0])
    expected_rows = case[1] if isinstance(case[1], list) else json.loads(case[1])

    ir_verdict, first_difference = compare_ir_payloads(approved_ir, produced_ir)
    if executed_rows is None:
        value_verdict, value_difference = "UNMEASURED", None
    else:
        value_verdict, value_difference = compare_expected_rows(
            expected_rows, executed_rows, grain_keys=grain_keys,
            decimal_policies=decimal_policies)

    if provider_dispatch_count > 0:
        # The assertion, applied FIRST: whatever the comparisons said, a compiler attempt that
        # dispatched to a provider is a failed attempt — its determinism claim is false.
        outcome = "FAILED_DISPATCH_PRESENT"
    elif ir_verdict == "INPUT_IDENTITY_MOVED":
        outcome = "IR_INPUT_IDENTITY_MOVED"
    elif ir_verdict == "DIFFERED":
        outcome = "FAILED_IR"
    elif value_verdict == "UNMEASURED":
        outcome = "UNMEASURED"
    elif value_verdict == "DIFFERED":
        outcome = "FAILED_VALUES"
    else:
        outcome = "PASSED"

    conn.execute(
        "INSERT INTO recipe_compiler_eval_attempt (attempt_id, case_revision_hash, "
        "contract_hash, ir_comparison, value_comparison, first_difference_path, outcome, "
        "unmeasured_reason, provider_dispatch_count) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (attempt_id, case_revision_hash, contract_hash, ir_verdict, value_verdict,
         first_difference or value_difference, outcome,
         ("no execution result — a deployment with no execution seam cannot certify (step 0b)"
          if outcome == "UNMEASURED" else None),
         provider_dispatch_count))
    return outcome


# ── the issuer ──────────────────────────────────────────────────────────────────────────────────
def issue_compiler_certificate(
    conn, *, contract_hash: str, subject_identity_hash: str,
    certificate_kind: str = "AUTHORING_METHOD",
) -> str | None:
    """Mint a `method_certificate_revision` iff EVERY corpus case's latest attempt under this
    contract PASSED — and the corpus is NON-EMPTY. Returns the certificate id, or ``None``.

    ▲ The empty-corpus guard is the whole point: all-passed over zero cases is vacuous truth, and
    the certificate it would mint is exactly §10.4's failure — the strongest claim on the weakest
    evidence. ``None`` is the honest answer, and growing the corpus is the honest remedy (§21:
    an operator act).
    """
    cases = [c[0] for c in conn.execute(
        "SELECT case_revision_hash FROM recipe_compiler_eval_case").fetchall()]
    if not cases:
        return None
    for case in cases:
        latest = conn.execute(
            "SELECT outcome FROM recipe_compiler_eval_attempt "
            "WHERE case_revision_hash = %s AND contract_hash = %s "
            "ORDER BY evaluated_at DESC LIMIT 1", (case, contract_hash)).fetchone()
        if latest is None or latest[0] != "PASSED":
            return None

    certificate_revision_id = jcs_sha256({
        "certificate_kind": certificate_kind,
        "subject_identity_hash": subject_identity_hash,
        "contract_hash": contract_hash,
        "corpus": cases,
    })
    conn.execute(
        "INSERT INTO method_certificate_revision (certificate_revision_id, certificate_kind, "
        "subject_identity_kind, subject_identity_hash, contract_hash, corpus_hash, outcome, "
        "evidence_json) VALUES (%s, %s, %s, %s, %s, %s, 'CERTIFIED', %s::jsonb) "
        "ON CONFLICT (certificate_revision_id) DO NOTHING",
        (certificate_revision_id, certificate_kind, certificate_kind, subject_identity_hash,
         contract_hash, jcs_sha256({"cases": sorted(cases)}),
         json.dumps({"cases": sorted(cases)})))
    return certificate_revision_id
