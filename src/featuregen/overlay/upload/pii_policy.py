"""The PII allow-policy contract (verified-interfaces doc D14; DEFERRED-WORK A.34).

MAY a feature be built from a personal-data concept, for WHAT purpose, on WHOSE authority — as an
immutable, content-addressed declaration. The store is
:mod:`featuregen.overlay.upload.pii_policy_store`; the reader that consumes it is
``feature_assist._use_gate``.

**What this is the answer to.** ``PERSONAL_DATA_POLICY_REQUIRED`` is the one USE refusal whose
wording promises an artifact ("a governance owner must declare one"). Until now no such artifact
existed, so the promise was empty and five shipped financial-crime recipes were permanently refused
(A.34). This module is the artifact.

**SINGLE-APPROVER, by explicit user decision (D14).** One platform-admin declares
concept + purpose; the policy is ACTIVE immediately; one action revokes it. This deliberately
deviates from the four-eyes convention every other governed declaration on this branch follows —
the immutable who/when/purpose record is the control. Nothing here models a confirmer, and a
``confirmed_by`` field is absent rather than nullable, because a nullable one would read as a step
somebody forgot rather than a step nobody is asked for. Do not re-add one without a new user
decision.

**A CONCEPT, never a column.** A policy licenses a MEANING for the whole platform. Scoping it to a
column would mean the same personal data licensed on ``cust.dob`` and unlicensed on
``cust_hist.dob`` — a distinction nobody could defend, and one a reviewer could route around by
naming the other column.

**Protected characteristics are refused, in the words that say so.** ``protected_attribute`` /
``special_category`` / ``vulnerability_flag`` are ``structurally_unsuitable``: ECOA/fair-lending and
GDPR Article 9 have no allow switch. :func:`validate_policy_concept` refuses them with wording that
says NO policy can lift it — the one refusal that must never read as a missing setting.

**Identity (D1).** ``materialize_hash`` (RFC 8785 JCS) over content only; ``pup_`` + the hash is the
deterministic, CHECK-pinned revision id. Content is concept + purpose + STATUS + authority axes.
``approved_by`` / ``approved_at`` are provenance and stay OUT of identity (the §6.2 house split), so
a second admin declaring identical content reuses the revision and the CAS pointer records who made
it current.

**Revocation is a new revision.** ``status`` being content is what makes that true: revoking mints a
content-distinct revision and advances the pointer. Nothing is deleted, nothing is updated in place,
and re-approving with the identical purpose resolves back to the original revision id.

**Purpose is bounded free text in v1 (D14).** The bound is the part that must exist now — a purpose
that can grow without limit is a payload, not a declaration. A closed purpose taxonomy is the named
later refinement, and is the residual A.34 records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.concepts import (
    CONCEPT_REGISTRY,
    is_personal_data,
    is_protected_characteristic,
)
from featuregen.overlay.upload.source_selection import (
    PolicyProvenanceV1,
    PolicyValidationError,
)

_PII_USE_POLICY_CONTRACT = "pii_use_policy_revision_v1"

#: Deterministic id prefix for a PII allow-policy revision (the ``cpr_``/``pbr_``/``dtp_`` family).
PII_USE_POLICY_ID_PREFIX = "pup_"

#: The purpose bound, pinned in BOTH places that can enforce it — here and the 1056 CHECK. The
#: minimum exists so "ok" cannot be a lawful-basis record; the maximum exists so a purpose stays a
#: sentence a reviewer reads, never a document a writer smuggles through a governance field.
MIN_PURPOSE_LEN = 8
MAX_PURPOSE_LEN = 300

_WHITESPACE = re.compile(r"\s+")


class PiiUsePolicyStatus(StrEnum):
    """The two states a policy revision can declare.

    ``REVOKED`` is a STATE OF A REVISION, not the absence of one: revoking records that somebody
    withdrew a specific declaration, which is a materially different fact from nobody ever having
    made one. The gate treats both as "not licensed"; the surfaces do not, and must not."""

    ACTIVE = "active"
    REVOKED = "revoked"


def policy_eligible_concepts() -> tuple[str, ...]:
    """Every concept a PII allow-policy may name — the registry's ``pii`` sensitivity class, sorted.

    Derived from the registry, never a hand list: a pii concept added tomorrow is licensable the
    same day, and a concept whose sensitivity is corrected stops being licensable without anyone
    remembering this module exists."""
    return tuple(sorted(name for name in CONCEPT_REGISTRY if is_personal_data(name)))


def validate_policy_concept(name: object) -> str:
    """The single gate on WHAT a policy may license. Returns the validated concept name.

    Three refusals, and the wording of each is the point:

    * a PROTECTED CHARACTERISTIC is refused with words that say no policy can ever allow it. This
      is the one case where a hopeful message would be a lie — ECOA/fair-lending and GDPR Article 9
      have no allow switch, and ``_use_gate`` already refuses such an operand as
      ``structurally_unsuitable`` before it ever reaches the personal-data class;
    * a concept the registry does not carry cannot be licensed, because the gate would never look
      for it — a policy on a name nothing classifies is a record that authorizes nothing;
    * a concept that IS in the registry but is not personal data needs no policy at all, and
      minting one would imply the platform was refusing it.
    """
    if not isinstance(name, str) or not name.strip():
        raise PolicyValidationError("a data-use policy must name a concept")
    concept_name = name.strip().lower()
    if is_protected_characteristic(concept_name):
        raise PolicyValidationError(
            f"{concept_name!r} is a protected characteristic (ECOA / GDPR Article 9). NO data-use "
            f"policy can allow it as a model input — this is a property of the attribute, not a "
            f"missing approval. Use a legitimate business driver instead, or correct the concept "
            f"if the column is not one")
    if concept_name not in CONCEPT_REGISTRY:
        raise PolicyValidationError(
            f"{concept_name!r} is not a concept in the registry, so nothing would ever consult a "
            f"policy about it. Data-use policies are declared per CONCEPT, not per column")
    if not is_personal_data(concept_name):
        raise PolicyValidationError(
            f"{concept_name!r} is not personal data, so no policy is needed to build features from "
            f"it — the feature use gate never refuses it. Only the registry's pii-classed concepts "
            f"take a data-use policy")
    return concept_name


def normalize_purpose(raw: object) -> str:
    """Normalize + bound the purpose text.

    Whitespace is collapsed before the bound is applied and before the content hash is taken, so
    "AML  screening" and "AML screening" are the SAME declaration rather than two revisions of it.
    Control characters are refused outright: a purpose is a sentence a human reads on a review
    card, and an embedded newline or NUL is either an accident or an attempt to make the card lie
    about its own length."""
    if not isinstance(raw, str):
        raise PolicyValidationError("purpose must be text")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw if ch not in " \t\n\r"):
        raise PolicyValidationError(
            "purpose must be plain readable text: control characters are not permitted in a "
            "governance declaration")
    purpose = _WHITESPACE.sub(" ", raw).strip()
    if len(purpose) < MIN_PURPOSE_LEN:
        raise PolicyValidationError(
            f"purpose must say what the data will be USED for, in at least {MIN_PURPOSE_LEN} "
            f"characters (for example 'AML transaction monitoring'). A lawful-basis record that "
            f"states no purpose records nothing")
    if len(purpose) > MAX_PURPOSE_LEN:
        raise PolicyValidationError(
            f"purpose must be at most {MAX_PURPOSE_LEN} characters; got {len(purpose)}. A purpose "
            f"is a sentence a reviewer reads, not a document")
    return purpose


@dataclass(frozen=True, slots=True)
class PiiUsePolicyRevisionV1:
    """One immutable PII allow-policy revision for ONE concept.

    ``status`` is CONTENT (module docstring): that is what makes revocation a new revision rather
    than an update, and it is why an approve/revoke/re-approve cycle resolves back to the first
    revision id — content-addressed identity, working exactly as it does for every other revision
    store on this branch."""

    concept_name: str
    purpose: str
    status: PiiUsePolicyStatus = PiiUsePolicyStatus.ACTIVE
    provenance: PolicyProvenanceV1 = field(default_factory=PolicyProvenanceV1)
    content_hash: str = ""
    revision_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "concept_name", validate_policy_concept(self.concept_name))
        object.__setattr__(self, "purpose", normalize_purpose(self.purpose))
        try:
            object.__setattr__(self, "status", PiiUsePolicyStatus(
                self.status.value if isinstance(self.status, PiiUsePolicyStatus)
                else str(self.status).strip().lower()))
        except ValueError as exc:
            raise PolicyValidationError(
                f"status must be one of {sorted(m.value for m in PiiUsePolicyStatus)}") from exc
        if not isinstance(self.provenance, PolicyProvenanceV1):
            raise PolicyValidationError("provenance must be a PolicyProvenanceV1")
        content_hash = materialize_hash(self.content_payload())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "revision_id", f"{PII_USE_POLICY_ID_PREFIX}{content_hash}")

    @property
    def active(self) -> bool:
        return self.status is PiiUsePolicyStatus.ACTIVE

    def content_payload(self) -> dict[str, Any]:
        """WHAT was declared, under WHAT class of authority. Never who filed it, never when."""
        return {
            "contract": _PII_USE_POLICY_CONTRACT,
            "concept_name": self.concept_name,
            "purpose": self.purpose,
            "status": self.status.value,
            "authority": self.provenance.authority_axes(),
        }
