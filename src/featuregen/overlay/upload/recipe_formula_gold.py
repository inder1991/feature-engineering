"""Immutable, reviewable corpus definition for the Formula-v1 provider gate."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from featuregen.overlay.upload.recipe_formula_shadow import content_hash

CORPUS_VERSION = "recipe-formula-gold-v1"


@dataclass(frozen=True, slots=True)
class FormulaGoldCase:
    case_id: str
    recipe_id: str
    case_kind: str
    case_input: dict[str, Any]
    expected: dict[str, Any]
    repeat_group: str | None = None


def _clean(recipe_id: str, index: int) -> FormulaGoldCase:
    role = "mcc" if recipe_id == "merchant_mcc_diversity" else "facility"
    entity = "merchant" if recipe_id == "merchant_mcc_diversity" else "obligor"
    return FormulaGoldCase(
        case_id=f"{recipe_id}-clean-{index:02d}",
        recipe_id=recipe_id,
        case_kind="clean",
        case_input={
            "variant": index,
            "operand_role": role,
            "grain_entity": entity,
            "window_days": 30 + index,
            "catalog_ref": f"gold::{recipe_id}.operand_{index:02d}",
        },
        expected={
            "disposition": "RESOLVED",
            "exact_match": True,
            "preservation_ok": True,
            "blocking_detected": False,
        },
        repeat_group=f"{recipe_id}-stability",
    )


_BLOCKING_CODES = (
    "MISSING_REQUIRED_OPERAND",
    "WRONG_SLOT_DIRECTION",
    "FILTER_INTENT_MISMATCH",
    "WINDOW_INTENT_MISMATCH",
)


def _adversarial(code: str) -> FormulaGoldCase:
    return FormulaGoldCase(
        case_id=f"adversarial-{code.lower()}",
        recipe_id="merchant_mcc_diversity",
        case_kind="adversarial",
        case_input={"fault": code},
        expected={
            "disposition": "NEEDS_REVIEW",
            "exact_match": False,
            "preservation_ok": False,
            "blocking_detected": True,
            "blocking_code": code,
        },
    )


FORMULA_GOLD_CASES: tuple[FormulaGoldCase, ...] = (
    *tuple(_clean("merchant_mcc_diversity", index) for index in range(1, 11)),
    *tuple(_clean("obligor_facility_count", index) for index in range(1, 11)),
    *tuple(_adversarial(code) for code in _BLOCKING_CODES),
    FormulaGoldCase(
        "adversarial-output-policy",
        "merchant_mcc_diversity",
        "adversarial",
        {"fault": "OUTPUT_POLICY_UNRESOLVED"},
        {"disposition": "NEEDS_REVIEW", "exact_match": False,
         "preservation_ok": False, "blocking_detected": True},
    ),
    FormulaGoldCase(
        "adversarial-operand-preservation",
        "obligor_facility_count",
        "adversarial",
        {"fault": "OPERAND_NOT_PRESERVED"},
        {"disposition": "REJECTED", "exact_match": False,
         "preservation_ok": False, "blocking_detected": True},
    ),
    FormulaGoldCase(
        "adversarial-malformed-response",
        "merchant_mcc_diversity",
        "adversarial",
        {"fault": "MALFORMED_PROVIDER_RESPONSE"},
        {"disposition": "TECHNICAL_FAILURE", "exact_match": False,
         "preservation_ok": False, "blocking_detected": True},
    ),
    FormulaGoldCase(
        "adversarial-dispatch-failure",
        "obligor_facility_count",
        "adversarial",
        {"fault": "DISPATCH_FAILURE"},
        {"disposition": "TECHNICAL_FAILURE", "exact_match": False,
         "preservation_ok": False, "blocking_detected": True},
    ),
)

CORPUS_CONTENT_HASH = content_hash([asdict(case) for case in FORMULA_GOLD_CASES])


def validate_formula_gold_corpus() -> None:
    ids = [case.case_id for case in FORMULA_GOLD_CASES]
    if len(ids) != len(set(ids)):
        raise ValueError("formula gold case ids must be unique")
    for recipe_id in ("merchant_mcc_diversity", "obligor_facility_count"):
        if sum(
            case.case_kind == "clean" and case.recipe_id == recipe_id
            for case in FORMULA_GOLD_CASES
        ) < 10:
            raise ValueError(f"formula gold corpus needs 10 clean cases for {recipe_id}")
    observed_codes = {
        case.expected.get("blocking_code")
        for case in FORMULA_GOLD_CASES
        if case.expected.get("blocking_code")
    }
    if observed_codes != set(_BLOCKING_CODES):
        raise ValueError("formula gold corpus does not cover the blocking critic vocabulary")


validate_formula_gold_corpus()
