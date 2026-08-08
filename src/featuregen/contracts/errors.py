from __future__ import annotations


class ConcurrencyError(Exception):
    """Raised when expected_version != the stream's current stream_version (OCC)."""


class ProjectionApplyError(Exception):
    """Raised by a fail-closed projection that cannot apply an event; carries the
    affected aggregate so the runner can mark it `degraded` and block its commands."""

    def __init__(self, aggregate: str, aggregate_id: str, reason: str) -> None:
        self.aggregate, self.aggregate_id, self.reason = aggregate, aggregate_id, reason
        super().__init__(f"{aggregate}:{aggregate_id}: {reason}")


class SchemaValidationError(Exception):
    """Raised by SchemaRegistry.validate on a schema mismatch."""


class AttestedSchemaValidationError(SchemaValidationError):
    """A schema failure whose author ATTESTS a value-free description of what went wrong.

    **Why a type and not an argument.** `featuregen.intake.llm._safe_reason` rebuilds a repair
    complaint from a jsonschema failure's STRUCTURE (`$.columns[3].unit: failed 'enum'`) because
    `ValidationError.message` embeds the offending instance value, and that value has not been
    through the §9.4 egress guard. A hand-written validator has no such structure to rebuild from,
    so every one of its failures collapses to one generic constant — and a repair prompt carrying
    no information is a provider call spent for nothing.

    This is the one way past that, and it is a TYPE so that the exemption is enumerable:
    `grep -rn AttestedSchemaValidationError src/` lists every site that has ever claimed it. An
    attribute duck-typed onto a plain error somewhere far away would be an invisible exemption,
    and `_safe_reason` deliberately refuses to honour one.

    **What `llm_safe_reason` promises, and who it promises it to.** The string is re-prompted to
    the provider (rendered into the repair turn by `llm_claude._wire_prompt`) AND persisted
    verbatim — into `llm_call.repair_attempts`, and into `llm_dispatch.redacted_input` wherever the
    caller is dispatch-audited. Neither sink re-scans, and `assert_llm_safe` runs once, before the
    FIRST dispatch, over `redacted_intent`/`catalog_metadata` only — so nothing downstream will
    catch a value that rides out here. The author is the last check.

    So the rule for anything interpolated into `llm_safe_reason` is narrow, and deliberately
    narrower than "it looks harmless":

    * **Author literals** — text written in the source file. Always fine.
    * **Closed in-code vocabularies** (a module-level `frozenset` of contract tokens). Fine: they
      are schema-authored, fixed, and typically already on the wire inside the output schema.
    * **NOT model-supplied text**, even a ref the model itself just proposed. `_path_is_schema_declared`
      already suppresses exactly this class from the structural pointer; an attestation must not
      re-admit through the front door what that guard turns away at the back.
    * **NOT catalog text** — column/table refs, labels, samples. That it also egressed inside
      `catalog_metadata` is a property of the CALLER, not of this string.

    The MESSAGE (`str(exc)`) is unconstrained and should stay as informative as it ever was: it is
    an in-process artefact for tracebacks and never reaches a wire, an audit column or a log.
    """

    def __init__(self, message: str, *, llm_safe_reason: str) -> None:
        super().__init__(message)
        self.llm_safe_reason = llm_safe_reason
