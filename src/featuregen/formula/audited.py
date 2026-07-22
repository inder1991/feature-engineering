"""The ONE audited seam TypedFormula authoring makes governed provider calls through.

The formula author (Task 9) and critic (Task 10) must NOT call ``record_llm_call`` directly or
re-implement the egress/schema/repair security boundary. They call ``audited_formula_call``, which
DELEGATES to the overlay's ``drive_audited_structured_call`` — the same egress sanitizer + schema
registry + durable fresh-connection llm_call audit + bounded repair/retry every overlay LLM node
already rides — and:

* threads the caller's ``authoring_run_id`` as the audit run bucket (instead of the hard-coded
  enrichment bucket) so every provider call an authoring run makes is queryable per run; and
* surfaces the FULL disposition the internal outcome carries — the immutable ``llm_call_ref``, the
  physical provider-call count, and provider usage — not just the validated output dict.

An EGRESS-BLOCKED call (a payload the guard refuses) yields ``output=None`` but is STILL audited:
``record_egress_block=True`` writes a content-free llm_call row for the block, so the block itself
is evidenced with a real ``llm_call_ref``.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext
from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call


@dataclass(frozen=True, slots=True)
class AuditedCallResult:
    """The full disposition of one governed authoring call.

    ``output`` — the validated output dict, or None on egress block / provider failure. ``None``
    output NEVER means "no audit": ``llm_call_ref`` is the immutable llm_call row (the call, or the
    block, was recorded). ``provider_calls`` is the PHYSICAL provider requests issued (0 when
    blocked before dispatch; 1 + repairs/retries otherwise). ``usage`` is provider-reported cost
    metadata (input/output tokens…), {} when nothing was dispatched."""
    output: dict | None
    llm_call_ref: str | None
    provider_calls: int
    usage: dict


def audited_formula_call(conn, client: LLMClient, *, authoring_run_id: str, task: str,
                         prompt_id: str, schema_id: str, instruction: str,
                         catalog_metadata: dict,
                         actor: IdentityEnvelope | None = None,
                         prompt_version: int = 1, schema_version: int = 1,
                         dispatch_audit: DispatchAuditContext | None = None,
                         cacheable_metadata_keys: tuple[str, ...] = ()) -> AuditedCallResult:
    """Run one governed authoring call and return its full disposition (see ``AuditedCallResult``).

    Delegates to ``drive_audited_structured_call`` — never re-implementing the egress/schema/repair
    boundary and never touching ``record_llm_call`` — threading ``authoring_run_id`` as the audit
    run bucket and requesting ``record_egress_block=True`` so a blocked payload is still audited
    (the block yields ``output=None`` but a real ``llm_call_ref``). ``prompt_version`` /
    ``schema_version`` / ``dispatch_audit`` / ``cacheable_metadata_keys`` pass straight through with
    the same defaults the overlay seam uses."""
    res = drive_audited_structured_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, instruction=instruction, actor=actor,
        prompt_version=prompt_version, schema_version=schema_version,
        dispatch_audit=dispatch_audit, cacheable_metadata_keys=cacheable_metadata_keys,
        run_id=authoring_run_id, record_egress_block=True)
    return AuditedCallResult(output=res.output, llm_call_ref=res.llm_call_ref,
                             provider_calls=res.provider_calls, usage=res.usage)
