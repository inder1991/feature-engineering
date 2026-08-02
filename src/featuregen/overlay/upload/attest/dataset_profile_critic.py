"""Independent, PROPOSAL-BLIND critic for Pass-B dataset-profile claims (profile Task 4).

Built on the :mod:`attest.concept_critic` template, with the one difference the plan names: this
critic is **proposal-blind**. The concept critic is shown the assignment and asked whether the
evidence supports it; that framing is right for a refute-oriented audit of a LABEL, but wrong for an
OPERATIONAL classification, where seeing the proposal is exactly what produces agreement bias. Here
the model is shown only the table's bounded evidence and asked to classify it INDEPENDENTLY; the
comparison against the proposal happens OUTSIDE the prompt, in :func:`critique_profile_claim`.

Order of authority, deliberately fixed (the concept critic's, adapted):

1. Deterministic contradictions run FIRST and elsewhere — :func:`table_synth.profile_contradictions`
   refutes before this module is ever called, so a contradicted claim is never dispatched.
2. The model answers a CLOSED classification (an `AuthorityRole` / `TemporalStorageModel` member, or
   the literal ``unknown``) — never a free-text verdict, so an off-vocabulary answer has no channel.
3. The comparison is code-side: agreement upholds, disagreement refutes, ``unknown`` abstains
   (upholding, because absence of independent support is not evidence against — the same
   refute-oriented rule the concept critic follows).

SCOPE, deliberately narrow: `authority_role`, `temporal_storage_model`, and an AMBIGUOUS high-impact
role claim. Descriptions and low-impact fields stay on the normal one-model path — a second paid
pass over a table description buys nothing and would double Pass-B's spend for display prose.

The critic critiques LLM proposals only. It never sees, and its consumers never touch, a
human-confirmed or source-attested profile value.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call
from featuregen.overlay.upload.profile_vocab import (
    AuthorityRole,
    TemporalStorageModel,
    normalize_authority_role,
    normalize_temporal_storage_model,
    profile_vocabulary_fingerprint,
)
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
)

if TYPE_CHECKING:
    from featuregen.contracts.envelopes import IdentityEnvelope
    from featuregen.intake.llm import LLMClient

logger = logging.getLogger(__name__)

PROFILE_CRITIC_VERSION = 1
PROFILE_CRITIC_RESULT_TYPE = "dataset_profile_critique"
PROFILE_CRITIC_RUN_ID = "dataset-profile-critic"
PROFILE_CRITIC_TASK = "overlay.pass_b.profile_critique"
PROFILE_CRITIC_PROMPT_ID = "dataset_profile_critique_v1"
PROFILE_CRITIC_SCHEMA_ID = "dataset_profile_critique"

#: The fields this critic is allowed to veto. Everything else stays one-model.
CRITIC_FIELDS: frozenset[str] = frozenset({"authority_role", "temporal_storage_model"})

_VOCAB = {
    "authority_role": (AuthorityRole, normalize_authority_role),
    "temporal_storage_model": (TemporalStorageModel, normalize_temporal_storage_model),
}

#: Closed reason vocabulary. Deterministic contradiction codes ride separately (they refute before
#: this module is reached); these explain THIS critic's own path to its disposition.
PROFILE_CRITIC_REASON_CODES = frozenset({
    "independent_agrees",
    "independent_disagrees",
    "independent_unknown",
    "critic_unavailable",
    "critic_invalid_result",
})


class ProfileCriticDisposition(StrEnum):
    """``UPHELD`` keeps the proposal (agreement, or an honest abstention — the refute-oriented rule:
    absence of independent support is not evidence against). ``REFUTED`` evicts it: an independent
    read of the SAME evidence reached a DIFFERENT closed answer."""

    UPHELD = "upheld"
    REFUTED = "refuted"


@dataclass(frozen=True, slots=True)
class ProfileCriticResultV1:
    dataset_ref: str
    field: str
    proposed_value: str
    independent_value: str | None
    disposition: ProfileCriticDisposition
    reason_codes: tuple[str, ...]
    structured_result_id: str | None
    llm_call_ref: str | None


def _instruction(field: str) -> str:
    vocab, _normalize = _VOCAB[field]
    members = ", ".join(sorted(m.value for m in vocab))
    subject = ("how AUTHORITATIVE this copy of the dataset is"
               if field == "authority_role" else "how this dataset STORES HISTORY")
    return (
        f"Given ONLY the table evidence provided, classify {subject}. Choose exactly one of: "
        f"{members}. Answer 'unknown' when the evidence does not settle it — an honest 'unknown' "
        "is always preferred to a guess. You are NOT being shown anyone else's answer; classify "
        "the evidence on its own terms."
    )


def _payload(dataset_ref: str, field: str, context: Mapping[str, object],
             cited_refs: Sequence[str]) -> dict[str, object]:
    """The bounded, PROPOSAL-BLIND egress payload. The proposed value is deliberately ABSENT — that
    is the whole design — and so is any hint of it (no `shape_conflicts`, no prior verdicts)."""
    payload: dict[str, object] = {
        "logical_ref": dataset_ref,
        "table": str(context.get("table") or ""),
        "evidence_refs": [str(r) for r in cited_refs],
        "column_roster": list(context.get("column_roster") or []),
    }
    for key in ("table_definition", "business_context"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = value.strip()[:600]
    return payload


def _input_hash(payload: dict[str, object], *, field: str, context_revision: str) -> str:
    """The replay identity: the EXACT context bytes, the prompt/schema versions, and the profile
    vocabulary fingerprint (a changed admissible-answer set is a changed question)."""
    return canonical_hash({
        "critic_version": PROFILE_CRITIC_VERSION,
        "prompt_id": PROFILE_CRITIC_PROMPT_ID,
        "schema_id": PROFILE_CRITIC_SCHEMA_ID,
        "field": field,
        "profile_vocabulary_fingerprint": profile_vocabulary_fingerprint(),
        "context_revision": context_revision,
        "input": payload,
    })


def _closed_reasons(codes: Sequence[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for code in codes:
        if code in PROFILE_CRITIC_REASON_CODES and code not in seen:
            seen.append(code)
    return tuple(seen)


def _result(dataset_ref, field, proposed, independent, reasons, result_id, call_ref
            ) -> ProfileCriticResultV1:
    upheld = independent is None or independent == proposed
    return ProfileCriticResultV1(
        dataset_ref=dataset_ref, field=field, proposed_value=proposed,
        independent_value=independent,
        disposition=(ProfileCriticDisposition.UPHELD if upheld
                     else ProfileCriticDisposition.REFUTED),
        reason_codes=_closed_reasons(reasons), structured_result_id=result_id,
        llm_call_ref=call_ref)


def critique_profile_claim(
    conn,
    client: LLMClient | None,
    *,
    dataset_ref: str,
    field: str,
    proposed_value: str,
    context: Mapping[str, object],
    cited_refs: Sequence[str],
    context_revision: str,
    actor: IdentityEnvelope | None = None,
) -> ProfileCriticResultV1:
    """Independently classify one profile claim and COMPARE OUTSIDE THE PROMPT.

    Fail-soft by construction, exactly like the concept critic: no client, a provider fault, a
    blocked dispatch or an off-vocabulary answer all resolve to an honest UPHELD abstention with a
    closed reason code — an advisory critic never aborts the Pass-B run that hosts it, and never
    evicts a plausible proposal on an infrastructure failure. A byte-identical re-ask replays from
    the structured-result store with no dispatch at all."""
    if field not in CRITIC_FIELDS:
        raise ValueError(f"{field!r} is outside this critic's scope ({sorted(CRITIC_FIELDS)})")
    payload = _payload(dataset_ref, field, context, cited_refs)
    input_hash = _input_hash(payload, field=field, context_revision=context_revision)

    stored = find_structured_result(
        conn, result_type=PROFILE_CRITIC_RESULT_TYPE, result_version=PROFILE_CRITIC_VERSION,
        input_content_hash=input_hash)
    if stored is not None:
        # Re-validate on replay: the store is immutable but the VOCABULARY is not, so a member that
        # has since been retired must be as unemittable on replay as on first derivation.
        raw = stored.output.get("independent_value")
        _vocab, normalize = _VOCAB[field]
        independent = normalize(raw) if isinstance(raw, str) else None
        return _result(dataset_ref, field, proposed_value, independent,
                       [str(c) for c in stored.output.get("reason_codes", ())
                        if isinstance(c, str)],
                       stored.structured_result_id, None)

    if client is None:
        return _result(dataset_ref, field, proposed_value, None, ["critic_unavailable"],
                       None, None)
    try:
        call = drive_audited_structured_call(
            conn, client, task=PROFILE_CRITIC_TASK, prompt_id=PROFILE_CRITIC_PROMPT_ID,
            schema_id=PROFILE_CRITIC_SCHEMA_ID, catalog_metadata=payload,
            instruction=_instruction(field), actor=actor, run_id=PROFILE_CRITIC_RUN_ID,
            record_egress_block=True)
    except Exception:  # noqa: BLE001 — advisory critic: a provider fault never aborts Pass B
        logger.warning("dataset profile critic call failed for %s/%s; treating as unavailable",
                       dataset_ref, field, exc_info=True)
        return _result(dataset_ref, field, proposed_value, None, ["critic_unavailable"],
                       None, None)
    if call.output is None:
        return _result(dataset_ref, field, proposed_value, None, ["critic_unavailable"],
                       None, call.llm_call_ref)
    raw = call.output.get("classification")
    _vocab, normalize = _VOCAB[field]
    if not isinstance(raw, str):
        return _result(dataset_ref, field, proposed_value, None, ["critic_invalid_result"],
                       None, call.llm_call_ref)
    if raw.strip().lower() == "unknown":
        independent, reason = None, "independent_unknown"
    else:
        independent = normalize(raw)
        if independent is None:
            return _result(dataset_ref, field, proposed_value, None, ["critic_invalid_result"],
                           None, call.llm_call_ref)
        reason = ("independent_agrees" if independent == proposed_value
                  else "independent_disagrees")
    result_id = None
    if call.llm_call_ref:
        stored_row = record_structured_result(
            conn, result_type=PROFILE_CRITIC_RESULT_TYPE,
            result_version=PROFILE_CRITIC_VERSION, input_content_hash=input_hash,
            output={"independent_value": independent, "reason_codes": [reason]},
            producer_kind="llm_call", producer_ref=call.llm_call_ref,
            authority={"authority": "llm_advisory", "logical_ref": dataset_ref, "field": field})
        result_id = stored_row.structured_result_id
    return _result(dataset_ref, field, proposed_value, independent, [reason], result_id,
                   call.llm_call_ref)
