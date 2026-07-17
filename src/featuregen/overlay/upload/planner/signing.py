"""Phase-3B.4 D8 — ed25519 DETACHED signing for the 3C-enablement-gate artifact (§10.7, F10).

The gate is computed by an EVALUATOR. "The evaluator cannot sign its own PASS" is a real security
property only under ASYMMETRIC signing: a symmetric HMAC (the ``security/audit.py`` model) lets any
key-holder — including the evaluator process — forge a signature, so detachment would be merely
procedural. So the gate artifact is signed with **ed25519**:

  * the PRIVATE key is held by a SEPARATE signing authority, OUTSIDE the evaluator's process
    (``sign_report`` runs there);
  * only the PUBLIC key is a config input (``FEATUREGEN_INTENT_GATE_PUBLIC_KEY``), read by the
    evaluator + CI to VERIFY (``verify_report``), and NEVER embedded in the artifact;
  * the signature is DETACHED into a sidecar file next to the report;
  * verification is fail-CLOSED (unset key → refuse) and yields a nonzero process exit on any failure.

We follow ``security/audit.py`` ONLY for the config-key-resolution + fail-closed pattern, never its
symmetric primitive.
"""
from __future__ import annotations

import base64
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

from featuregen.config import get_settings


class GateKeyNotConfigured(RuntimeError):
    """The trusted public key is unset — verification refuses rather than trust an unsigned gate."""


def generate_keypair() -> tuple[str, str]:
    """(private_pem, public_pem). The signing authority keeps the private half; only the public half
    is distributed to evaluators/CI as ``FEATUREGEN_INTENT_GATE_PUBLIC_KEY``. Ops/test helper."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public_pem = private.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    return private_pem, public_pem


def sign_report(report_bytes: bytes, private_key_pem: str) -> bytes:
    """SIGNER-side: produce a detached ed25519 signature over the canonical report bytes. Runs in the
    signing authority's process, never the evaluator's (the evaluator has no private key)."""
    key = load_pem_private_key(private_key_pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("intent-gate signing key must be ed25519")
    return key.sign(report_bytes)


def verify_signature(report_bytes: bytes, signature: bytes, public_key_pem: str) -> bool:
    """Verify a detached signature against an EXPLICIT public key. True/False only (never raises on a
    bad signature) — an invalid signature, a tampered report, or the wrong key all return False."""
    try:
        key = load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(signature, report_bytes)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _trusted_public_key() -> str:
    """Resolve the trusted public key from config, fail-CLOSED (unset → refuse; never a default)."""
    key = get_settings().intent_gate_public_key
    if not key:
        raise GateKeyNotConfigured(
            "FEATUREGEN_INTENT_GATE_PUBLIC_KEY is not configured; refusing to verify the "
            "3C enablement-gate signature without a trusted public key (fail-closed).")
    return key


def verify_report(report_bytes: bytes, signature: bytes,
                  trusted_public_key_pem: str | None = None) -> bool:
    """Evaluator/CI-side verification. Uses the config-resolved trusted key by default (fail-closed if
    unset); an explicit key can be passed for tests. Returns True ONLY for an intact report signed by
    the trusted authority."""
    return verify_signature(report_bytes, signature,
                            trusted_public_key_pem or _trusted_public_key())


def sidecar_path(report_path: str | Path) -> Path:
    return Path(str(report_path) + ".sig")


def write_signature_sidecar(report_path: str | Path, signature: bytes) -> Path:
    """Write the DETACHED signature (base64) beside the report; the report file itself is untouched."""
    path = sidecar_path(report_path)
    path.write_text(base64.b64encode(signature).decode() + "\n")
    return path


def read_signature_sidecar(report_path: str | Path) -> bytes:
    return base64.b64decode(sidecar_path(report_path).read_text().strip())


def verify_report_file(report_path: str | Path,
                       trusted_public_key_pem: str | None = None) -> bool:
    """Verify a report file against its ``.sig`` sidecar. ALL failure modes → False (fail-closed): a
    missing/unreadable report OR sidecar, a tampered payload, the wrong key, or an UNSET trusted key
    (``GateKeyNotConfigured``) — verification never trusts by default and never raises out of here."""
    try:
        signature = read_signature_sidecar(report_path)
        report_bytes = Path(report_path).read_bytes()
    except (OSError, ValueError):
        return False
    try:
        return verify_report(report_bytes, signature, trusted_public_key_pem)
    except GateKeyNotConfigured:
        return False    # no trusted key configured → refuse (fail-closed), never a default-trust pass


def verify_cli(report_path: str | Path, trusted_public_key_pem: str | None = None) -> int:
    """A process exit code: 0 iff the report verifies, 1 otherwise (§10.7 — nonzero exit on any
    verify failure). A failed machine gate is not overridable: a False here must block the pipeline."""
    return 0 if verify_report_file(report_path, trusted_public_key_pem) else 1
