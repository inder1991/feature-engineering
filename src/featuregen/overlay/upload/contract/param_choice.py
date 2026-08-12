"""Hypothesis-chosen recipe parameters (Task 4b) — the "it looks hardcoded" defect.

23 grounded recipes offer 147 authored parameterisations and the platform emits the same 23
first-in-list defaults for every question ever asked. Parameters are exactly where a hypothesis
SHOULD differentiate: "customers churn when activity drops off" and "detect structuring within a
reporting period" want different windows from the same recipe — the registry already says both are
valid.

Discipline:

* CLOSED SELECTION — the model picks values from the authored tuples (sent as the menu), and every
  answer is re-validated against ``Template.params`` before it can reach grounding; ``_bind_params``
  then enforces it a second time. The model cannot invent a setting.
* ABSTAIN = TODAY — a hypothesis that implies nothing yields no override, and no-override grounding
  is byte-identical to the historical first-allowed-value default. Flag-off and abstain are the
  same bytes.
* ONE dispatch per build for the MISSES only — per-template results are content-addressed in
  ``structured_result`` keyed by (template menu, hypothesis, prompt version), so a repeat
  hypothesis replays every template free, and a registry edit to one template re-asks exactly that
  one. An abstain is STORED (empty params) so it never re-asks either.
* Identity is already handled — ``semantic_parameter_binding_hash`` covers the bound params, and
  the feature NAME carries the window, so the same recipe under two hypotheses is two identities,
  never a mutation of one card.
"""
from __future__ import annotations

import logging

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
)
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

PARAM_CHOICE_RESULT_TYPE = "param_choice"
PARAM_CHOICE_RESULT_VERSION = 1

PARAM_CHOICE_TASK = "overlay.contract.param_choice"
PARAM_CHOICE_PROMPT_ID = "param_choice"
PARAM_CHOICE_PROMPT_VERSION = 1
PARAM_CHOICE_SCHEMA_ID = "param_choice"
PARAM_CHOICE_RUN_ID = "param-choice"

_INSTRUCTION = (
    "The analyst's objective and a menu of recipe parameters are given. For each recipe in the "
    "menu, choose parameter values ONLY where the objective clearly implies them (a stated "
    "timescale, a reporting period, a measure the question names) — copy `template_id`, `param` "
    "and `value` EXACTLY from the menu. OMIT every parameter the objective does not clearly "
    "decide: no answer means the authored default applies, which is always safe. Never invent a "
    "value not listed in the menu."
)


def _choosable_menu(template) -> dict[str, tuple]:
    """The parameters worth asking about: more than one authored value. A single-value param has
    no choice to make and never reaches the model."""
    return {key: allowed for key, allowed in template.params.items() if len(allowed) > 1}


def _menu_wire(menu: dict[str, tuple]) -> dict[str, list[str]]:
    return {key: [str(v) for v in allowed] for key, allowed in menu.items()}


def _template_key(template, menu: dict[str, tuple], redacted_hypothesis: str) -> str:
    return canonical_hash({
        "version": "param-choice-input-v1",
        "prompt_id": PARAM_CHOICE_PROMPT_ID,
        "prompt_version": PARAM_CHOICE_PROMPT_VERSION,
        "template_id": template.id,
        "menu": _menu_wire(menu),
        "hypothesis": redacted_hypothesis,
    })


def _validated(menu: dict[str, tuple], raw_params: dict) -> dict:
    """Map the model's string answers back onto the AUTHORED, typed values — matching on the string
    form, returning the registry's own object. Off-menu keys and values are dropped silently: the
    default is always safe, and a partial answer is a partial override."""
    chosen: dict = {}
    for key, value in raw_params.items():
        allowed = menu.get(key)
        if allowed is None:
            continue
        for candidate in allowed:
            if str(candidate) == str(value):
                chosen[key] = candidate
                break
    return chosen


def choose_params(conn, client, *, templates, redacted_hypothesis: str,
                  call_ledger=None) -> dict[str, dict]:
    """The hypothesis's parameter overrides, per template id — ONLY where the model chose and the
    registry agrees. Missing template = no override = today's defaults (``_bind_params`` re-guards
    regardless). Replays are per-template; at most ONE provider call per build covers every miss."""
    menus = {t.id: m for t in templates if (m := _choosable_menu(t))}
    if not menus:
        return {}
    by_id = {t.id: t for t in templates}
    overrides: dict[str, dict] = {}
    misses: dict[str, dict[str, tuple]] = {}
    keys: dict[str, str] = {}
    for template_id, menu in menus.items():
        key = _template_key(by_id[template_id], menu, redacted_hypothesis)
        keys[template_id] = key
        stored = find_structured_result(
            conn, result_type=PARAM_CHOICE_RESULT_TYPE,
            result_version=PARAM_CHOICE_RESULT_VERSION, input_content_hash=key)
        if stored is not None:
            counters.incr("overlay.param_choice.replayed")
            chosen = _validated(menu, dict(stored.output).get("params") or {})
            if chosen:
                overrides[template_id] = chosen
            continue
        misses[template_id] = menu
    if not misses or client is None:
        return overrides
    if call_ledger is not None and not call_ledger.charge():
        counters.incr("overlay.param_choice.call_ceiling")
        return overrides

    from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call

    try:
        call = drive_audited_structured_call(
            conn, client, task=PARAM_CHOICE_TASK,
            prompt_id=f"{PARAM_CHOICE_PROMPT_ID}_v{PARAM_CHOICE_PROMPT_VERSION}",
            schema_id=PARAM_CHOICE_SCHEMA_ID,
            # `objective` — the roundtrip-prose class (arrives here already redacted);
            # `parameter_menu` — structural: the registry's own authored tuples, repo text.
            catalog_metadata={"objective": redacted_hypothesis,
                              "parameter_menu": {tid: _menu_wire(m)
                                                 for tid, m in sorted(misses.items())}},
            instruction=_INSTRUCTION, run_id=PARAM_CHOICE_RUN_ID,
            record_egress_block=True)
    except Exception:  # noqa: BLE001 — advisory variety, never load-bearing
        logger.warning("param-choice dispatch failed; authored defaults apply", exc_info=True)
        return overrides
    raw_by_template: dict[str, dict] = {tid: {} for tid in misses}
    for item in (call.output or {}).get("choices") or []:
        if not isinstance(item, dict):
            continue
        tid = item.get("template_id")
        if tid in raw_by_template:
            raw_by_template[tid][str(item.get("param"))] = item.get("value")
    for template_id, raw in raw_by_template.items():
        chosen = _validated(misses[template_id], raw)
        # Store the VALIDATED answer — including the honest empty (abstain) — so a repeat
        # hypothesis never re-asks about a template the model already declined to decide.
        record_structured_result(
            conn, result_type=PARAM_CHOICE_RESULT_TYPE,
            result_version=PARAM_CHOICE_RESULT_VERSION,
            input_content_hash=keys[template_id],
            output={"params": {k: str(v) for k, v in chosen.items()}},
            producer_kind="llm_call",
            producer_ref=call.llm_call_ref or "param_choice:unrecorded")
        counters.incr("overlay.param_choice.chosen" if chosen
                      else "overlay.param_choice.abstained")
        if chosen:
            overrides[template_id] = chosen
    return overrides
