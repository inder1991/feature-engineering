"""Spec §14 — the four CLOSED failure vocabularies, plus the refusal exception.

Every governed failure in Spec A names a member of exactly one of these enums.
No code outside them may be used, and **no code appears in two of them**: a
value in two vocabularies cannot be typed unambiguously, so a handler would be
forced back to comparing raw strings — precisely the defect §14 exists to stop.

The four vocabularies answer four different questions:

- ``CompilationRefusalCode`` — compile refused; nothing was generated. Decided
  from governed metadata alone, before any row is read.
- ``PublicationRefusalCode`` — the group computed, but must not be published.
  A publication decision is **neither** a compilation refusal **nor** a runtime
  gate; it gets its own enum because an earlier draft typed these values into
  the compilation enum and then papered over the mismatch with a raw-string
  comparison.
- ``ValidationGateCode`` — a BLOCKING gate inside the generated pipeline (§9).
  These fire during execution, on real rows.
- ``ValidationFindingCode`` — an L0/L1/L2 finding (§11), non-blocking by itself.

Membership tests use ``==``, never ``>=``: a superset assertion permits
arbitrary extra codes and so does not test a closed vocabulary at all.
"""
from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CompilationRefusalCode",
    "MaterializationRefused",
    "PublicationRefusalCode",
    "ValidationFindingCode",
    "ValidationGateCode",
]


class CompilationRefusalCode(StrEnum):
    """Refusals raised or returned during compilation — nothing is generated.

    Decided from governed metadata only. A condition that can be known only by
    looking at actual rows is NOT here; it is a ``ValidationGateCode``.
    """

    # Admission (§1.2) — the authoring run and its terminal event.
    AUTHORING_RUN_INCOMPLETE = "AUTHORING_RUN_INCOMPLETE"
    TERMINAL_PAYLOAD_TAMPERED = "TERMINAL_PAYLOAD_TAMPERED"
    NOT_RESOLVED = "NOT_RESOLVED"
    FORMULA_HASH_MISMATCH = "FORMULA_HASH_MISMATCH"
    # BR-6: the compiler consumes exactly Formula-v1 today. A formula claiming any other schema
    # version is refused at admission — never silently compiled under v1 semantics. Lifting this
    # is an ENGINE capability decision (capability_v2.EngineCapabilityV1), not a default.
    FORMULA_SCHEMA_UNSUPPORTED = "FORMULA_SCHEMA_UNSUPPORTED"
    AXES_MISMATCH = "AXES_MISMATCH"
    INTENT_HASH_MISMATCH = "INTENT_HASH_MISMATCH"

    # Read scope and safety classification.
    READ_SCOPE_INSUFFICIENT = "READ_SCOPE_INSUFFICIENT"
    PROHIBITED_INPUT = "PROHIBITED_INPUT"
    #: a physical read names a column the governed catalog does not describe — §11's L1 would
    #: call it COLUMN_ABSENT, but compile must not emit a read nobody governs.
    COLUMN_NOT_GOVERNED = "COLUMN_NOT_GOVERNED"

    # Physical resolution and join planning (§3).
    AMBIGUOUS_TABLE_NAME = "AMBIGUOUS_TABLE_NAME"
    JOIN_PATH_NOT_VERIFIED = "JOIN_PATH_NOT_VERIFIED"
    JOIN_PATH_DENIED_BY_READ_SCOPE = "JOIN_PATH_DENIED_BY_READ_SCOPE"
    GRAIN_PATH_NOT_GOVERNED = "GRAIN_PATH_NOT_GOVERNED"
    JOIN_FANOUT_UNSUPPORTED = "JOIN_FANOUT_UNSUPPORTED"
    JOIN_CARDINALITY_UNKNOWN = "JOIN_CARDINALITY_UNKNOWN"

    # Entity population / spine declaration (§4).
    SPINE_SOURCE_NOT_DECLARED = "SPINE_SOURCE_NOT_DECLARED"
    SPINE_DECLARATION_REJECTED_BY_FACTS = "SPINE_DECLARATION_REJECTED_BY_FACTS"

    # Time: partitions must be DECLARED, never inferred (§3.4).
    PARTITION_MAPPING_NOT_DECLARED = "PARTITION_MAPPING_NOT_DECLARED"
    PHYSICAL_SCHEMA_NOT_RESOLVED = "PHYSICAL_SCHEMA_NOT_RESOLVED"
    SOURCE_ENGINE_UNSUPPORTED = "SOURCE_ENGINE_UNSUPPORTED"
    AVAILABILITY_TIME_NOT_GOVERNED = "AVAILABILITY_TIME_NOT_GOVERNED"

    # Output contract and physical typing.
    OUTPUT_TYPE_NOT_GOVERNED = "OUTPUT_TYPE_NOT_GOVERNED"
    PHYSICAL_TYPE_UNSUPPORTED = "PHYSICAL_TYPE_UNSUPPORTED"
    MULTIPLE_MATERIALIZATION_CONTRACTS = "MULTIPLE_MATERIALIZATION_CONTRACTS"
    PARTITION_IDENTITY_UNKNOWN = "PARTITION_IDENTITY_UNKNOWN"
    UNACCOUNTED_LOGICAL_REF = "UNACCOUNTED_LOGICAL_REF"

    #: D-4: what was compiled is not what the human's governed plan said. The frozen plan envelope
    #: (`semantic_option_decision.binding_plan`, migration 1066, carried to compilation on the work
    #: item by 1068) names the source table, the population, the read set, the grain and the window
    #: the option was SERVED with; compilation derives its own answers from governed catalog facts,
    #: and this code fires when the two disagree. It is a COMPILATION refusal — decided from
    #: governed metadata alone, before a row is read — and it is never a substitution: the envelope
    #: is validated against, never used to overwrite what compilation computed, exactly as
    #: `fold_frozen_binding_plan` applies `BINDING_PLAN_DIVERGENCE` to itself. A run that carries no
    #: envelope (every work item written before migration 1068) cannot raise it.
    PLAN_ENVELOPE_DIVERGENCE = "PLAN_ENVELOPE_DIVERGENCE"


class PublicationRefusalCode(StrEnum):
    """Publication decisions — the group may have computed, but must not publish.

    Kept apart from ``CompilationRefusalCode`` on purpose: these are decided
    after compilation, against the target environment and the group binding,
    and typing them as compilation refusals is what forced an earlier draft
    into a raw-string fallback.
    """

    #: The environment's publish mechanism was never proven by the executable
    #: capability probe (§16) — publishing on an assumed capability is banned.
    CAPABILITY_UNPROVEN = "CAPABILITY_UNPROVEN"
    #: A logical feature name's contract hash differs from its group binding.
    GROUP_BINDING_CONFLICT = "GROUP_BINDING_CONFLICT"
    #: The probe proved the environment offers no supported publish mechanism.
    PUBLISH_MECHANISM_UNSUPPORTED = "PUBLISH_MECHANISM_UNSUPPORTED"


class ValidationGateCode(StrEnum):
    """BLOCKING gates inside the GENERATED pipeline (§9) — failures on real rows.

    Every member here is discoverable only during execution. ``SPINE_NON_DETERMINISTIC``
    is the clearest case: an unresolved tie depends on the actual rows, so it is
    a runtime discovery and deliberately NOT a compilation refusal.
    """

    # Output shape and contract.
    KEY_NOT_UNIQUE = "KEY_NOT_UNIQUE"
    MISSING_FEATURE_COLUMN = "MISSING_FEATURE_COLUMN"
    UNEXPECTED_COLUMN = "UNEXPECTED_COLUMN"
    WRONG_COLUMN_TYPE = "WRONG_COLUMN_TYPE"
    WRONG_NULLABILITY = "WRONG_NULLABILITY"
    SCHEMA_HASH_MISMATCH = "SCHEMA_HASH_MISMATCH"

    # Staging manifests — the group's atomic-publication evidence.
    MISSING_STAGING_MANIFEST = "MISSING_STAGING_MANIFEST"
    STALE_STAGING_MANIFEST = "STALE_STAGING_MANIFEST"
    DUPLICATE_STAGING_MANIFEST = "DUPLICATE_STAGING_MANIFEST"

    # Computation integrity.
    IR_HASH_MISMATCH = "IR_HASH_MISMATCH"
    INCOMPLETE_COMPUTATION = "INCOMPLETE_COMPUTATION"
    FORBIDDEN_NUMERIC = "FORBIDDEN_NUMERIC"
    OVERFLOW_VIOLATION = "OVERFLOW_VIOLATION"

    # Directional cross-catalog join integrity.
    JOIN_AMPLIFICATION = "JOIN_AMPLIFICATION"

    # Spine integrity — all three need the rows to be known.
    SPINE_INCOMPLETE = "SPINE_INCOMPLETE"
    SPINE_DUPLICATE_KEY = "SPINE_DUPLICATE_KEY"
    SPINE_NON_DETERMINISTIC = "SPINE_NON_DETERMINISTIC"

    # Run wiring.
    RUN_PARAMETERS_MISSING = "RUN_PARAMETERS_MISSING"
    PROJECT_INTEGRITY = "PROJECT_INTEGRITY"


class ValidationFindingCode(StrEnum):
    """L0/L1/L2 validation findings (§11) — non-blocking by themselves."""

    # L0 — the generated project itself.
    PROJECT_DOES_NOT_BUILD = "PROJECT_DOES_NOT_BUILD"
    PROJECT_HASH_MISMATCH = "PROJECT_HASH_MISMATCH"
    PIPELINE_NOT_CONSTRUCTIBLE = "PIPELINE_NOT_CONSTRUCTIBLE"
    #: The interpreter that proved the build is not the environment the artifact PINS ITSELF to
    #: (``requirements.lock``, rendered from ``ClusterInventoryV1.engine_versions`` and inside the
    #: project hash). Added for DEFERRED-WORK A.42: without it the one Phase G failure that fails
    #: OPEN had no name — L0 imported the project in whatever interpreter it was handed, never
    #: compared the two, and reported ``PASSED``, so "the build was proven" could be a proof about
    #: a different environment than the one the artifact declares. It is a FINDING rather than a
    #: refusal because L0 reports; and it is a finding rather than ``status="error"`` because
    #: something WAS observed and a report that carries no findings cannot name what.
    ENGINE_VERSION_MISMATCH = "ENGINE_VERSION_MISMATCH"

    # L1 — the physical inputs the run will actually read.
    COLUMN_ABSENT = "COLUMN_ABSENT"
    COLUMN_TYPE_MISMATCH = "COLUMN_TYPE_MISMATCH"
    PARTITION_ABSENT = "PARTITION_ABSENT"
    READ_DENIED = "READ_DENIED"
    #: L1's third question was NOT ANSWERED, and this deployment declared in advance that its engine
    #: has no authorization model to answer it (``FEATUREGEN_MATERIALIZE_DECLARE_NO_AUTHORIZATION_``
    #: ``MODEL``, user decision 2026-08-15). The ONE finding this platform emits at
    #: :attr:`~featuregen.materialize.validation.FindingSeverity.WARNING`: it does not stop the run —
    #: that is what the declaration accepts — and it is recorded so that a run which proceeded on an
    #: unverified read scope is distinguishable, forever, from one whose read scope the engine
    #: actually answered. Without the declaration an unanswerable read scope is not this code: it is
    #: ``MetastoreReadScopeUnanswerable``, and it still fails the run closed.
    READ_SCOPE_UNVERIFIED = "READ_SCOPE_UNVERIFIED"

    #: Anything unrecognized → ``FindingClass.UNCLASSIFIED``, which FAILS CLOSED.
    UNKNOWN_FINDING = "UNKNOWN_FINDING"


class MaterializationRefused(Exception):
    """A governed refusal, carrying a TYPED code — never a bare string.

    Raised for compilation refusals and publication refusals. Runtime gate
    failures (``ValidationGateCode``) and findings (``ValidationFindingCode``)
    are *reported* by the generated pipeline, not raised through this type, so
    they are deliberately not accepted as ``code``.

    ``detail`` is operator-facing text: counts, types, hashes and locations
    only — never data values.
    """

    __slots__ = ("code", "detail")

    def __init__(
        self,
        code: CompilationRefusalCode | PublicationRefusalCode,
        detail: str = "",
    ) -> None:
        # Enforced, not merely annotated: the whole point of §14 is that a
        # refusal's code is a member of a closed vocabulary, so a raw string
        # (or a gate/finding code) must not be able to reach `.code`.
        if not isinstance(code, CompilationRefusalCode | PublicationRefusalCode):
            raise TypeError(
                "MaterializationRefused.code must be a CompilationRefusalCode or "
                f"PublicationRefusalCode, got {type(code).__name__}"
            )
        super().__init__(f"{code.value}: {detail}" if detail else code.value)
        self.code = code
        self.detail = detail
