"""BR-4 — the temporal compiler: PIT text is RENDERED from the typed contract, never authored.

The defect class this retires: 153 legacy recipes carry hand-written PIT prose with `{placeholder}`
tokens, two of which referenced parameters their recipe never declared — drift no reviewer caught
because prose reviews don't diff parameter names. Under V2 the human-readable PIT line is a PURE
RENDERING of ``TemporalSpecV2`` + the resolved parameter selection: an unresolved placeholder is
impossible (the renderer asserts none survive), and a recipe whose temporal contract is missing a
load-bearing piece does not get worse prose — it gets ``blocked`` with the blocker NAMED.

The four banking time shapes render DISTINCTLY and cannot share a declaration:

* ``event``               — a trailing observation window over a named event clock;
* ``as_of`` / ``effective_interval`` — point-in-time / effective-dated STATE via a snapshot policy;
* ``contractual_future``  — a FORWARD horizon ("maturing in (cutoff, cutoff+90d]"), which the
  renderer words as a future interval and never as a trailing window;
* ``pre_decision``        — real-time pre-authorization state, MINUTE-grained, and only under a
  governed pre-decision feed authority — the merchant_mcc_diversity rule: batch data does not get
  real-time wording by vibes.

Knowledge time is a first-class clause: a source that can be corrected or arrive late must declare
its knowledge-time role and late-arrival behavior, or the compile is blocked — "we used the latest
restated numbers at an earlier cutoff" is the bug this refuses to leave unstated.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from featuregen.overlay.upload.recipe_contract_v2 import (
    RecipeDefinitionV2,
    TemporalSpecV2,
)

COMPILED_TEMPORAL_VERSION = "compiled-temporal-v1"

# Closed blocker codes — the compiler's whole failure vocabulary. BR-7's readiness fold consumes
# these verbatim (a blocked temporal contract is FORMULA_BLOCKED with these names attached).
BLOCKER_EVENT_ROLE_UNBOUND = "event_time_role_unbound"
BLOCKER_WINDOW_UNBOUND = "window_unbound"
BLOCKER_SNAPSHOT_POLICY_MISSING = "snapshot_policy_missing"
BLOCKER_EFFECTIVE_ROLE_UNBOUND = "business_effective_role_unbound"
BLOCKER_PRE_DECISION_AUTHORITY_UNPROVEN = "pre_decision_authority_unproven"
BLOCKER_PRE_DECISION_NOT_MINUTE_GRAINED = "pre_decision_not_minute_grained"
BLOCKER_KNOWLEDGE_TIME_MISSING = "knowledge_time_missing"


@dataclass(frozen=True, slots=True)
class CompiledTemporalV1:
    """The compiler's output: ``compiled`` with a placeholder-free ``pit_text``, or ``blocked``
    with every missing piece NAMED. There is no third state and no partial text — a blocked
    contract renders nothing rather than something misleading."""

    status: str                    # "compiled" | "blocked"
    pit_text: str                  # "" when blocked
    blockers: tuple[str, ...]      # () when compiled
    anchor_kind: str
    window_token: str              # "90d" / "60min" / "" — the canonical suffix form


def _window_token(spec: TemporalSpecV2, selection: Mapping[str, object]) -> str | None:
    """The canonical window token (30d / 60min), or None when a declared window has no bound
    value — which is a BLOCKER, not a blank."""
    if spec.window_unit == "none":
        return ""
    if not spec.window_parameter:
        return None
    value = selection.get(spec.window_parameter)
    if value is None:
        return None
    suffix = "d" if spec.window_unit == "days" else "min"
    return f"{value}{suffix}"


def compile_temporal(definition: RecipeDefinitionV2, *,
                     selection: Mapping[str, object] | None = None,
                     correctable_source: bool = False) -> CompiledTemporalV1:
    """Compile one recipe's temporal contract at one parameter selection (default: the reviewed
    default variant). ``correctable_source`` is the caller's declaration that the bound source can
    restate or late-deliver rows — it forces the knowledge-time clause or blocks."""
    from featuregen.overlay.upload.recipe_variants import resolve_variant

    spec = definition.temporal
    if selection is None:
        selection = dict(resolve_variant(definition).selection)
    operand_roles = {op.role: op for op in definition.operands}
    blockers: list[str] = []

    token = _window_token(spec, selection)
    if token is None:
        blockers.append(BLOCKER_WINDOW_UNBOUND)

    if spec.anchor_kind in ("event", "pre_decision"):
        role = operand_roles.get(spec.event_time_role)
        if role is None or role.operand_class not in ("event_timestamp", "as_of_timestamp"):
            blockers.append(BLOCKER_EVENT_ROLE_UNBOUND)
    if spec.anchor_kind == "as_of" and not spec.snapshot_policy.strip():
        blockers.append(BLOCKER_SNAPSHOT_POLICY_MISSING)
    if spec.anchor_kind == "effective_interval" and not spec.business_effective_role.strip():
        blockers.append(BLOCKER_EFFECTIVE_ROLE_UNBOUND)
    if spec.anchor_kind == "pre_decision":
        # Real-time wording is EARNED: a governed pre-decision feed authority and minute grain,
        # or the recipe does not get to claim it runs before the decision point.
        if not spec.temporal_authority_ref.strip():
            blockers.append(BLOCKER_PRE_DECISION_AUTHORITY_UNPROVEN)
        if spec.window_unit != "minutes":
            blockers.append(BLOCKER_PRE_DECISION_NOT_MINUTE_GRAINED)
    if correctable_source and not (spec.knowledge_time_role.strip()
                                   and spec.late_arrival_policy.strip()):
        blockers.append(BLOCKER_KNOWLEDGE_TIME_MISSING)

    if blockers:
        return CompiledTemporalV1(status="blocked", pit_text="", blockers=tuple(blockers),
                                  anchor_kind=spec.anchor_kind, window_token=token or "")

    cutoff_bracket = "]" if spec.cutoff_inclusivity == "inclusive" else ")"
    if spec.anchor_kind == "event":
        text = (f"trailing {token} observation window over {spec.event_time_role!s} events: "
                f"(cutoff − {token}, cutoff{cutoff_bracket}, values knowable strictly at or "
                "before the cutoff")
    elif spec.anchor_kind == "as_of":
        text = (f"point-in-time state as of the cutoff via {spec.snapshot_policy}: the latest "
                "known state at or before the cutoff, never forward-looking"
                + (f", over a {token} lookback" if token else ""))
    elif spec.anchor_kind == "effective_interval":
        text = (f"effective-dated state valid at the cutoff per {spec.business_effective_role} "
                "validity, never a later restatement"
                + (f", within a {token} observation window" if token else ""))
    elif spec.anchor_kind == "contractual_future":
        text = (f"FORWARD contractual horizon: obligations falling in (cutoff, cutoff + {token}"
                f"{cutoff_bracket} under {spec.future_horizon_policy}, read from contract terms "
                "knowable at or before the cutoff — a future ladder, never a trailing "
                "observation window")
    else:  # pre_decision
        text = (f"real-time pre-decision state (t − {token}, t) strictly BEFORE the "
                f"authorization/decision point, under the governed pre-decision feed "
                f"{spec.temporal_authority_ref}")

    if spec.knowledge_time_role.strip():
        text += (f"; knowledge time per {spec.knowledge_time_role}"
                 + (f", late arrivals per {spec.late_arrival_policy}"
                    if spec.late_arrival_policy.strip() else ""))
    if spec.calendar_policy.strip():
        text += f"; business calendar {spec.calendar_policy}"
    if spec.timezone_policy.strip():
        text += f"; timezone {spec.timezone_policy}"

    if "{" in text or "}" in text:
        raise AssertionError(f"rendered PIT text leaked a placeholder: {text!r}")
    return CompiledTemporalV1(status="compiled", pit_text=text, blockers=(),
                              anchor_kind=spec.anchor_kind, window_token=token or "")
