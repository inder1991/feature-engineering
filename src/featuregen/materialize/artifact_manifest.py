"""C-D4 — an EXTERNAL control-plane manifest for a sealed artifact's files.

**``GENERATED.lock`` is left alone**, and that is a decision with teeth. ``read_lock`` enforces a
strict TWO-KEY top level — ``set(parsed) != {"compilation", "generated_project_hash"}`` raises
(``identity.py:446``) — so adding a manifest key to the lock would make every existing lock
unreadable. Worse, ``run_l0`` calls ``_lock_of`` as its first statement inside the chain's
transaction and does not catch, so an extended lock would abort the compile rather than produce a
finding.

**The manifest lives ONLY in PostgreSQL. It must never be written into the project tree.** The
cluster-side integrity gate hashes every file in the tree except ``GENERATED.lock`` and ``*.pyc``
(``nodes_gate.py:443-449``), so a manifest sidecar, a restore marker or a content-store receipt
placed beside the source would fail ``PROJECT_INTEGRITY`` on the cluster. ``materialize_to`` also
refuses a non-empty target, so there is nowhere in the tree such a file could legally appear.

**There is no cycle, and the plan's justification for one is only half right.**
``generated_project_hash`` skips the lock unconditionally (``identity.py:385``), so bytes added to
the lock never re-enter the hash: files could be stored first and a manifest naming their content
references minted afterwards with no circularity. The only genuine self-reference would be a pointer
to the LOCK'S OWN bytes, which cannot live inside itself — and that case only arises if the manifest
is required to cover the lock. It is not: :func:`manifest_for` refuses to include it, because the
lock is the one file whose content is decided by the manifest's own existence.

**Bytes are verified on write, on retrieval and on execution.** Three points, because a digest
checked only at write proves the bytes were right once, and the failure this guards against is
corruption or substitution afterwards.

**Scope.** This freezes the manifest TYPE and the Postgres store. Calling it from the compile chain
is S7 (migration 1078's stage), not S0.5 — and it would write into the exact transaction C-D0 is
chartered to split.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.materialize.identity import GENERATED_LOCK_FILENAME

__all__ = [
    "ARTIFACT_MEDIA_TYPE",
    "ArtifactFileEntryV1",
    "ArtifactManifestV1",
    "ManifestIntegrityError",
    "manifest_for",
    "verify_bytes",
]

#: Every file this slice renders is text. Recorded per entry rather than assumed, because the
#: manifest is what a retrieval path decides how to decode by, and "assume UTF-8" is how a binary
#: artifact later becomes mojibake nobody notices.
ARTIFACT_MEDIA_TYPE = "text/plain; charset=utf-8"


class ManifestIntegrityError(Exception):
    """Stored bytes do not match the digest recorded for them."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactFileEntryV1:
    """One generated file: where it belongs, what it hashes to, and how to fetch it back."""

    path: str
    sha256: str
    byte_length: int
    media_type: str
    content_reference: str

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("a manifest entry with no path names no file")
        if self.path == GENERATED_LOCK_FILENAME:
            raise ValueError(
                f"{GENERATED_LOCK_FILENAME} cannot be a manifest entry: the lock's content is "
                f"decided by this manifest's own existence, so a pointer to its bytes would be a "
                f"pointer to something not yet decided. It is also the one file "
                f"`generated_project_hash` skips, which is why no cycle exists for any other")
        if len(self.sha256) != 64 or not all(c in "0123456789abcdef" for c in self.sha256):
            raise ValueError(
                f"sha256 for {self.path!r} is not a 64-character lower-case hex digest: "
                f"{self.sha256!r}")
        if self.byte_length < 0:
            raise ValueError(f"byte_length for {self.path!r} is negative")
        if not self.content_reference.strip():
            raise ValueError(
                f"{self.path!r} has no content reference: the manifest would record that a file "
                f"exists without recording how to fetch it back, which is a catalogue rather than "
                f"a manifest")

    def identity_payload(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "byte_length": self.byte_length,
                "media_type": self.media_type, "content_reference": self.content_reference}


@dataclass(frozen=True, slots=True)
class ArtifactManifestV1:
    """Every file of one sealed artifact, EXTERNAL to the artifact itself."""

    artifact_id: str
    entries: tuple[ArtifactFileEntryV1, ...]

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ValueError("a manifest must name the artifact it describes")
        if not self.entries:
            raise ValueError(
                "a manifest with no entries describes no artifact, and would let an empty "
                "retrieval look like a complete one")
        paths = [e.path for e in self.entries]
        if len(set(paths)) != len(paths):
            raise ValueError(
                f"a path appears twice: {sorted(paths)}. Two entries for one file means two "
                f"answers about what its bytes are")
        object.__setattr__(
            self, "entries", tuple(sorted(self.entries, key=lambda e: e.path)))

    def entry_for(self, path: str) -> ArtifactFileEntryV1 | None:
        return next((e for e in self.entries if e.path == path), None)

    def identity_payload(self) -> dict[str, Any]:
        return {"artifact_id": self.artifact_id,
                "entries": [e.identity_payload() for e in self.entries]}


def manifest_for(
    artifact_id: str, files: Mapping[str, str], *,
    content_reference: Any,
) -> ArtifactManifestV1:
    """A manifest over ``files`` — ``GENERATED.lock`` deliberately excluded.

    Args:
        files: path → text, as ``SealedProject`` holds them.
        content_reference: called with each path to produce its immutable reference. Passed in so
            the manifest does not decide where bytes live; the Postgres store is the first slice,
            not the only possible one.
    """
    entries = tuple(
        ArtifactFileEntryV1(
            path=path, sha256=_sha256(text), byte_length=len(text.encode("utf-8")),
            media_type=ARTIFACT_MEDIA_TYPE, content_reference=content_reference(path))
        for path, text in sorted(files.items())
        if path != GENERATED_LOCK_FILENAME)
    return ArtifactManifestV1(artifact_id=artifact_id, entries=entries)


def verify_bytes(entry: ArtifactFileEntryV1, text: str, *, at: str) -> str:
    """Check ``text`` against ``entry``, or refuse. Returns the text so it can be used inline.

    ``at`` names the verification point — ``"write"``, ``"retrieval"`` or ``"execution"`` — because
    the three failures mean different things: at write the renderer disagrees with itself, at
    retrieval the store returned something else, and at execution the bytes changed after they were
    fetched.
    """
    digest = _sha256(text)
    if digest != entry.sha256:
        raise ManifestIntegrityError(
            f"{entry.path} failed its digest check at {at}: expected {entry.sha256}, computed "
            f"{digest}. The manifest and the bytes disagree, so neither may be trusted")
    length = len(text.encode("utf-8"))
    if length != entry.byte_length:
        raise ManifestIntegrityError(
            f"{entry.path} failed its length check at {at}: expected {entry.byte_length} bytes, "
            f"got {length}")
    return text
