"""S1C-3 — the shadow V2 parameter chooser: audited, content-addressed, evaluation-only.

The typed chooser over ``ParameterSpecV2`` menus. It runs INSIDE the telemetry worker only: its
choices are recorded on observations (``param_divergence`` gains the chooser's pick beside the
token-match) and are never served. That resolves the accuracy-measurement deadlock — chooser
accuracy against the S1C-1 corpus is computable in Stage 1C, and the SAME chooser is promoted to
serving in Stage 2 with its accuracy number already known.

Three disciplines, each load-bearing:

* **Closed selection.** The model is sent the authored menu and a schema that admits exactly
  ``{"pick": "<string>"}``; the menu-membership check runs CODE-SIDE after the audited call (the
  registry registers static schema bodies, so a per-menu enum cannot ride the schema). An off-menu
  answer is ``invalid_pick`` with an empty pick — recorded honestly, never retried beyond the
  repair the audited machinery itself runs.
* **Content addressing.** ``sha256("param_choice|" + prompt version + "|" + parameter + "|" +
  menu joined on \\x1f + "|" + hypothesis)``. Before ANY dispatch the address is looked up in the
  SHARED ``structured_result`` store (1039) — the same content-addressed replay surface the
  audited machinery's other callers (``contract/param_choice``, bridge criticism) already ride, so
  no new table ships. A hit replays free; ``chosen`` and ``invalid_pick`` are both stored (asking
  the same address again buys the same answer), ``unavailable`` NEVER is — a billing outage must
  not poison an address forever.
* **Fail-soft.** Any provider/infrastructure exception degrades to ``status="unavailable"`` —
  logged, never raised. The cluster's LLM stages fail closed on exhausted billing; the chooser
  records honest absence instead of failing the telemetry item that carries it.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
)
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

PARAM_CHOICE_PROMPT_VERSION = "param-choice-v1"

#: The chooser's OWN audit vocabulary — a NEW task key, distinct from the serving-path Task 4b
#: chooser (``overlay.contract.param_choice``), so the llm_call ledger can tell the shadow
#: measurement from the served template lane at a glance.
PARAM_CHOICE_TASK = "param_choice"
PARAM_CHOICE_PROMPT_ID = "param_choice_pick_v1"
PARAM_CHOICE_SCHEMA_ID = "param_choice_pick"
PARAM_CHOICE_RUN_ID = "param-choice-shadow"

#: The replay surface: the shared ``structured_result`` store, keyed on this pair plus the
#: chooser's full content address as ``input_content_hash``.
PARAM_CHOICE_RESULT_TYPE = "param_choice_shadow"
PARAM_CHOICE_RESULT_VERSION = 1

STATUS_CHOSEN = "chosen"
STATUS_INVALID_PICK = "invalid_pick"
STATUS_UNAVAILABLE = "unavailable"
_STATUSES = frozenset({STATUS_CHOSEN, STATUS_INVALID_PICK, STATUS_UNAVAILABLE})

_INSTRUCTION = (
    "The analyst's objective and ONE parameter's menu of authored values are given. Answer with "
    "the single menu value the objective most clearly implies (a stated timescale, a reporting "
    "period, a horizon the question names), copied EXACTLY as it appears in the menu, in `pick`. "
    "You must answer with exactly one value from the menu — never a value that is not listed."
)


@dataclass(frozen=True, slots=True)
class ParamChoiceV1:
    """One chooser verdict, self-validating: a ``chosen`` result MUST name a menu member, and a
    pickless status (``invalid_pick``/``unavailable``) MUST carry the honest empty pick."""

    parameter: str            # e.g. "window"
    menu: tuple[str, ...]     # the closed allowed_values, as strings
    pick: str                 # a menu member when status == "chosen", "" otherwise
    status: str               # closed: chosen | invalid_pick | unavailable
    content_address: str      # full sha256 hex over the chooser's input material
    prompt_version: str

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"param choice status {self.status!r} is not one of "
                             f"{sorted(_STATUSES)}")
        if self.status == STATUS_CHOSEN:
            if self.pick not in self.menu:
                raise ValueError(
                    f"a chosen pick must be a menu member; {self.pick!r} is not in {self.menu}")
        elif self.pick != "":
            raise ValueError(
                f"status {self.status!r} carries no pick; got {self.pick!r} — an answer that "
                "was not validated must not masquerade as one")


def param_choice_content_address(*, parameter: str, menu: Sequence[str],
                                 hypothesis: str) -> str:
    """The chooser's identity: one address per (menu, hypothesis, prompt version). \\x1f (unit
    separator) joins the menu so no menu member can collide with the material's own delimiters."""
    material = ("param_choice|" + PARAM_CHOICE_PROMPT_VERSION + "|" + parameter + "|"
                + "\x1f".join(menu) + "|" + hypothesis)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def choose_parameter(conn, *, llm, hypothesis: str, parameter: str,
                     menu: Sequence[str]) -> ParamChoiceV1:
    """One governed closed-selection choice — replayed free when the address has answered before.

    ``llm`` is the injected provider client (the worker never reads env; whatever scheduler
    constructs a real chooser resolves the flag and key outside). Never raises for a provider or
    store failure: the degraded outcome is an honest ``unavailable`` result.
    """
    menu = tuple(str(value) for value in menu)
    if not menu:
        raise ValueError("param choice needs a non-empty menu — an empty menu is a caller bug, "
                         "not a choice")
    address = param_choice_content_address(parameter=parameter, menu=menu, hypothesis=hypothesis)

    def _result(pick: str, status: str) -> ParamChoiceV1:
        return ParamChoiceV1(parameter=parameter, menu=menu, pick=pick, status=status,
                             content_address=address,
                             prompt_version=PARAM_CHOICE_PROMPT_VERSION)

    # The replay read runs in its OWN savepoint and never takes the caller down: without one, a DB
    # failure here poisons the telemetry item's whole transaction, and "never raises for a store
    # failure" was only true of the write half. A failed read (or an unparseable stored payload) is
    # a CACHE MISS — the chooser dispatches normally rather than degrading to `unavailable`,
    # because the provider was never asked.
    stored = None
    try:
        with conn.transaction():
            stored = find_structured_result(
                conn, result_type=PARAM_CHOICE_RESULT_TYPE,
                result_version=PARAM_CHOICE_RESULT_VERSION, input_content_hash=address)
    except Exception:  # noqa: BLE001 — fail-soft: a broken replay store must not take the item
        logger.warning("param-choice replay-store read failed for %s; proceeding as a cache miss",
                       address, exc_info=True)
    if stored is not None:
        try:
            output = dict(stored.output)
            replayed = _result(str(output.get("pick") or ""),
                               str(output.get("status") or STATUS_INVALID_PICK))
        except Exception:  # noqa: BLE001 — a corrupt stored payload is a miss, not a failure
            logger.warning("param-choice stored result for %s does not parse; proceeding as a "
                           "cache miss", address, exc_info=True)
        else:
            counters.incr("overlay.param_choice_shadow.replayed")
            return replayed

    from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call

    try:
        call = drive_audited_structured_call(
            conn, llm, task=PARAM_CHOICE_TASK, prompt_id=PARAM_CHOICE_PROMPT_ID,
            schema_id=PARAM_CHOICE_SCHEMA_ID,
            # `objective` — the roundtrip-prose class (arrives here already redacted);
            # `parameter_menu` — structural: the registry's own authored tuple, repo text.
            catalog_metadata={"objective": hypothesis,
                              "parameter_menu": {parameter: list(menu)}},
            instruction=_INSTRUCTION, run_id=PARAM_CHOICE_RUN_ID,
            record_egress_block=True)
    except Exception:  # noqa: BLE001 — fail-soft: evaluation must degrade, never take the item
        logger.warning("param-choice dispatch failed; recorded unavailable (never cached)",
                       exc_info=True)
        counters.incr("overlay.param_choice_shadow.unavailable")
        return _result("", STATUS_UNAVAILABLE)
    if call.output is None:
        # Egress-blocked, provider-failed, or repair-exhausted: no VALIDATED answer arrived. The
        # call (or its block) is already audited under its llm_call_ref; the address stays
        # uncached so a healthy provider can answer it later.
        counters.incr("overlay.param_choice_shadow.unavailable")
        return _result("", STATUS_UNAVAILABLE)

    raw_pick = str(call.output.get("pick") or "")
    if raw_pick in menu:
        pick, status = raw_pick, STATUS_CHOSEN
    else:
        pick, status = "", STATUS_INVALID_PICK
    try:
        with conn.transaction():    # same savepoint idiom as the read: a failed DB write caught
            record_structured_result(  # without one would still poison the caller's transaction
                conn, result_type=PARAM_CHOICE_RESULT_TYPE,
                result_version=PARAM_CHOICE_RESULT_VERSION, input_content_hash=address,
                output={"pick": pick, "status": status, "parameter": parameter,
                        "menu": list(menu)},
                producer_kind="llm_call",
                producer_ref=call.llm_call_ref or "param_choice:unrecorded")
    except Exception:  # noqa: BLE001 — the choice is valid even when the cache write is not
        logger.warning("param-choice replay-store write failed for %s; the choice is returned "
                       "uncached", address, exc_info=True)
    counters.incr(f"overlay.param_choice_shadow.{status}")
    return _result(pick, status)


__all__ = [
    "PARAM_CHOICE_PROMPT_ID",
    "PARAM_CHOICE_PROMPT_VERSION",
    "PARAM_CHOICE_RESULT_TYPE",
    "PARAM_CHOICE_RESULT_VERSION",
    "PARAM_CHOICE_RUN_ID",
    "PARAM_CHOICE_SCHEMA_ID",
    "PARAM_CHOICE_TASK",
    "ParamChoiceV1",
    "STATUS_CHOSEN",
    "STATUS_INVALID_PICK",
    "STATUS_UNAVAILABLE",
    "choose_parameter",
    "param_choice_content_address",
]
