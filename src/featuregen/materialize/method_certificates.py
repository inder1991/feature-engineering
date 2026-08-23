"""§9 piece 4 — the METHOD-LEVEL certificate reader, and the per-member production preview.

The question this answers is the one `current_evaluation_validity` cannot: *"was this exact
METHOD IDENTITY certified by the platform corpus, and is that certificate current?"* — keyed on
the sealed member's identity hash, so it answers for a novel LLM feature that has no recipe
expectation just as it does for a reviewed one.

Until step 9's evaluation programme ISSUES certificates the table is empty, the reader answers
``None``, and every member surfaces ``METHOD_CERTIFICATE_MISSING`` — §9's hard-block from day
one: absence never acts as permission, and earning the first certificate never makes the
platform stricter than it already was.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.overlay.upload import semantic_eligibility_reasons as R

__all__ = ["MethodCertificateV1", "current_method_certificate", "member_certificate_facts"]


@dataclass(frozen=True, slots=True)
class MethodCertificateV1:
    certificate_revision_id: str
    certificate_kind: str
    subject_identity_hash: str
    contract_hash: str
    corpus_hash: str
    outcome: str


def current_method_certificate(
    conn, *, certificate_kind: str, subject_identity_hash: str,
) -> MethodCertificateV1 | None:
    """The newest CERTIFIED certificate covering this exact subject, or ``None``.

    Exact-hash lookup, deliberately: a certificate for a DIFFERENT identity is not a weaker
    match, it is no match (§10.3 — matching both kinds against one hash was revision four's
    defect). Currency beyond "newest certified" — contract-hash staleness against the deployed
    world — is step 9's validity contract; until it exists, ``METHOD_CERTIFICATE_STALE`` cannot
    be emitted honestly and is not.
    """
    row = conn.execute(
        "SELECT certificate_revision_id, contract_hash, corpus_hash, outcome "
        "FROM method_certificate_revision "
        "WHERE certificate_kind = %s AND subject_identity_hash = %s AND outcome = 'CERTIFIED' "
        "ORDER BY issued_at DESC LIMIT 1",
        (certificate_kind, subject_identity_hash)).fetchone()
    if row is None:
        return None
    return MethodCertificateV1(
        certificate_revision_id=row[0], certificate_kind=certificate_kind,
        subject_identity_hash=subject_identity_hash, contract_hash=row[1],
        corpus_hash=row[2], outcome=row[3])


def member_certificate_facts(
    conn, *, sealed_artifact_id: str,
) -> dict[str, dict[str, Any]]:
    """Per sealed member: its method identity (or the honest absence) and its certificate state.

    The vocabulary codes returned here feed the decision service's §5 fold as member FACTS:

    * no method-identity row → ``METHOD_IDENTITY_UNRECORDED`` — §10.4's ruling: pre-companion
      provenance is permanently ineligible; a backfill would record today's identity against
      yesterday's bytes, a fabricated match, worse than an absent one;
    * an identity with no covering certificate → ``METHOD_CERTIFICATE_MISSING``.
    """
    members = conn.execute(
        "SELECT p.member_name, i.method_identity_hash, i.authoring_method "
        "  FROM sealed_artifact_member_provenance p "
        "  LEFT JOIN sealed_artifact_member_method_identity i "
        "    ON i.artifact_id = p.artifact_id AND i.member_name = p.member_name "
        " WHERE p.artifact_id = %s ORDER BY p.member_name",
        (sealed_artifact_id,)).fetchall()

    facts: dict[str, dict[str, Any]] = {}
    for member_name, identity_hash, authoring_method in members:
        if identity_hash is None:
            facts[member_name] = {
                "blockers": (R.METHOD_IDENTITY_UNRECORDED,),
                "method_identity_hash": None,
                "certificate": None,
            }
            continue
        certificate = current_method_certificate(
            conn, certificate_kind="AUTHORING_METHOD", subject_identity_hash=identity_hash)
        facts[member_name] = {
            "blockers": () if certificate is not None else (R.METHOD_CERTIFICATE_MISSING,),
            "method_identity_hash": identity_hash,
            "authoring_method": authoring_method,
            "certificate": certificate,
        }
    return facts
