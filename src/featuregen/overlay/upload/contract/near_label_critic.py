"""The near-label leakage critic — flag-only verdicts on surviving candidates (Task 3).

23 templates carry ``near_label=True`` and, until this module, the flag rendered one warning chip
and enforced NOTHING. The ``target_ref`` veto only rejects a candidate that BINDS the label column;
it cannot compare a 90-day-inactivity feature against a 90-day-inactivity label. This critic asks
that one fenced question — is this feature approximately the label itself? — and answers in a
CLOSED vocabulary with, deliberately, no token that reads as "cleared":

* ``too_close``  — the feature ≈ the label (same outcome, ≈ the same window). The only verdict
  with an effect, plus a bounded rationale for the card.
* ``no_finding`` — an ordinary predictor. NOT a clearance: in this platform an LLM output must
  never clear a design check (`_governed_read` and the gauntlet own clearance).
* ``abstain``    — cannot tell, or nothing to compare against. Abstention-as-designed-answer: no
  signed label window means every verdict is an honest abstain and NO model is called.

Discipline (architecture review + owner decisions, 2026-08-10):

* ORIGIN-BLIND — runs on EVERY surviving candidate, template-grounded and LLM-proposed alike.
  Safety checks are origin-blind, exactly as the gauntlet is; the template flag survives only as
  authoring documentation.
* FLAG-ONLY — verdicts annotate cards; nothing is removed. Turning ``too_close`` into a refusal is
  a later, explicit product decision. The whole pass sits behind ``FEATUREGEN_NEAR_LABEL_CRITIC``
  (default OFF, byte-identical).
* The label window is DATA, not prose: the intake build's signed reading
  (``contract_intent.target_window_days``, migration 1059). The hypothesis prose rides along
  already-redacted, for construct comparison only.
* Content-addressed replay through ``structured_result`` keyed by (candidate content, label
  material, prompt version) — the Task 2b seam; ledger-bounded spend; degrade to abstain, never
  block or 5xx.
"""
from __future__ import annotations

import logging
import os
from dataclasses import replace

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
)
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

NEAR_LABEL_RESULT_TYPE = "near_label_verdict"
NEAR_LABEL_RESULT_VERSION = 1

NEAR_LABEL_TASK = "overlay.contract.near_label"
NEAR_LABEL_PROMPT_ID = "near_label_critic"
NEAR_LABEL_PROMPT_VERSION = 1
NEAR_LABEL_SCHEMA_ID = "near_label_verdict"
NEAR_LABEL_RUN_ID = "near-label-critic"

VERDICTS = ("no_finding", "too_close", "abstain")
_MAX_RATIONALE = 300

_INSTRUCTION = (
    "You are a leakage critic. The analyst's objective (label) and ONE candidate feature are "
    "given. Answer exactly one question: is this feature approximately the label itself — does it "
    "measure (or near-deterministically encode) the same outcome over roughly the same time "
    "window? `too_close` ONLY when the feature is close to BEING the label (e.g. a days-since-"
    "last-activity feature with a 90-day window against a churn label defined as 90 days of "
    "inactivity). `no_finding` when it is an ordinary predictor of the label — being predictive "
    "is the point, not a finding. `abstain` when you cannot tell from what is given. You cannot "
    "clear a feature and `no_finding` is not a clearance. `rationale`: one bounded sentence a "
    "reviewer can act on."
)


def near_label_critic_enabled() -> bool:
    """Flag-only rollout, read at call time (the platform's pattern). Default OFF: byte-identical."""
    return os.environ.get("FEATUREGEN_NEAR_LABEL_CRITIC", "0") == "1"


def _abstain(idea, why: str):
    counters.incr("overlay.near_label.abstain")
    return replace(idea, near_label_verdict="abstain", near_label_rationale=why)


def _candidate_material(idea) -> dict:
    """What the critic reads of one candidate — its own card text and structure. For an LLM-origin
    idea the description is the model's; for a recipe idea it is the registry's intent line. Either
    way it is the SAME text the human reviews, so the critic and the reviewer see one artifact."""
    return {
        "name": idea.name,
        "description": idea.description or "",
        "aggregation": idea.aggregation or "",
        "window": idea.window or "",
    }


def _input_hash(*, material: dict, label_window_days: int, redacted_hypothesis: str) -> str:
    return canonical_hash({
        "version": "near-label-input-v1",
        "prompt_id": NEAR_LABEL_PROMPT_ID,
        "prompt_version": NEAR_LABEL_PROMPT_VERSION,
        "candidate": material,
        "label_window_days": label_window_days,
        "hypothesis": redacted_hypothesis,
    })


def _verdict_from_output(output: dict) -> tuple[str, str] | None:
    verdict = output.get("verdict")
    if verdict not in VERDICTS:
        return None
    rationale = output.get("rationale")
    rationale = rationale.strip()[:_MAX_RATIONALE] if isinstance(rationale, str) else ""
    return verdict, rationale


def annotate_near_label(conn, client, *, ideas, redacted_hypothesis: str,
                        label_window_days: int | None, call_ledger=None) -> list:
    """Annotate every surviving candidate with a near-label verdict. Returns NEW FeatureIdea
    objects (the input list is not mutated). Never raises and never removes: the worst outcome of
    any failure — no client, ceiling, fault, invalid output — is an honest ``abstain`` with a
    rationale that says why, which renders as today's warning chip.

    With no signed label window the whole pass is pure code (every verdict abstains, zero model
    calls) — the "near-label candidates withheld per the abstain rule" degrade of the intake spec,
    expressed in flag-only terms."""
    if label_window_days is None:
        return [_abstain(i, "no label window declared — sign a target window to run this check")
                for i in ideas]
    if client is None:
        return [_abstain(i, "critic unavailable") for i in ideas]

    from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call

    out = []
    for idea in ideas:
        material = _candidate_material(idea)
        key = _input_hash(material=material, label_window_days=label_window_days,
                          redacted_hypothesis=redacted_hypothesis)
        stored = find_structured_result(
            conn, result_type=NEAR_LABEL_RESULT_TYPE,
            result_version=NEAR_LABEL_RESULT_VERSION, input_content_hash=key)
        if stored is not None:
            parsed = _verdict_from_output(dict(stored.output))
            if parsed is not None:
                counters.incr("overlay.near_label.replayed")
                counters.incr(f"overlay.near_label.{parsed[0]}")
                out.append(replace(idea, near_label_verdict=parsed[0],
                                   near_label_rationale=parsed[1]))
                continue
            # An unusable stored verdict (vocabulary drift) degrades exactly like a fresh fault.
        if call_ledger is not None and not call_ledger.charge():
            out.append(_abstain(idea, "critic call ceiling reached"))
            continue
        try:
            call = drive_audited_structured_call(
                conn, client, task=NEAR_LABEL_TASK,
                prompt_id=f"{NEAR_LABEL_PROMPT_ID}_v{NEAR_LABEL_PROMPT_VERSION}",
                schema_id=NEAR_LABEL_SCHEMA_ID,
                # Egress classes, all pre-classified: `objective` is the roundtrip-prose class the
                # generation call already rides (the hypothesis arrives HERE already redacted);
                # `candidates` is structural-with-owned-scanning — the card text was produced by
                # the platform's own generation/registry paths and re-crosses the boundary as one
                # item; `label_window_days` is a structural integer.
                catalog_metadata={"objective": redacted_hypothesis,
                                  "candidates": [material],
                                  "label_window_days": label_window_days},
                instruction=_INSTRUCTION, run_id=NEAR_LABEL_RUN_ID,
                record_egress_block=True)
        except Exception:  # noqa: BLE001 — advisory pass; a fault must not sink the considered set
            logger.warning("near-label critic dispatch failed; abstaining", exc_info=True)
            out.append(_abstain(idea, "critic unavailable"))
            continue
        parsed = _verdict_from_output(dict(call.output)) if call.output is not None else None
        if parsed is None:
            out.append(_abstain(idea, "critic returned no usable verdict"))
            continue
        record_structured_result(
            conn, result_type=NEAR_LABEL_RESULT_TYPE,
            result_version=NEAR_LABEL_RESULT_VERSION, input_content_hash=key,
            output=dict(call.output), producer_kind="llm_call",
            producer_ref=call.llm_call_ref or "near_label:unrecorded")
        counters.incr("overlay.near_label.adjudicated")
        counters.incr(f"overlay.near_label.{parsed[0]}")
        out.append(replace(idea, near_label_verdict=parsed[0], near_label_rationale=parsed[1]))
    return out
