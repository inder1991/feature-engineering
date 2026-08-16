"""C-D4 — the Postgres content store the manifest points at.

**PostgreSQL, not a new object-store subsystem.** Every file this slice renders is text, the whole
project is kilobytes, and standing up an object store to hold it would add an availability
dependency to the compile path for no gain. The manifest names files by ``content_reference``
rather than by "row in this table", so the day a real object store is warranted only the reference
scheme changes.

**Content-addressed.** The reference IS the digest, so two artifacts that render byte-identical
files share one row and a re-render of unchanged bytes stores nothing new. That also makes the store
write-once in the only way that matters: a given reference always names the same bytes, because the
reference is computed from them.

**Verified on write and on retrieval.** :func:`store_file` re-derives the digest before inserting
and :func:`fetch_file` re-derives it after reading, so corruption between the two is caught at the
point it becomes visible rather than at the point it becomes damage.
"""
from __future__ import annotations

import hashlib

from featuregen.materialize.artifact_manifest import (
    ArtifactFileEntryV1,
    ManifestIntegrityError,
    verify_bytes,
)

__all__ = [
    "content_reference_for",
    "fetch_file",
    "store_file",
    "store_manifest",
]


def content_reference_for(text: str) -> str:
    """The immutable reference for these exact bytes — content-addressed, so it cannot go stale."""
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def store_file(conn, text: str) -> str:
    """Store ``text`` and return its content reference. Idempotent by construction."""
    reference = content_reference_for(text)
    conn.execute(
        "INSERT INTO generated_artifact_blob (content_reference, content, byte_length) "
        "VALUES (%s, %s, %s) ON CONFLICT (content_reference) DO NOTHING",
        (reference, text, len(text.encode("utf-8"))))
    return reference


def store_manifest(conn, manifest, files) -> None:
    """Store every file the manifest names, verifying each against its entry BEFORE writing.

    Verification at write catches the renderer disagreeing with itself — a manifest built from one
    dict and files taken from another. It is cheap and it is the only point where that particular
    mistake is still recoverable.
    """
    for entry in manifest.entries:
        if entry.path not in files:
            raise ManifestIntegrityError(
                f"the manifest names {entry.path!r} but the supplied files do not contain it: the "
                f"manifest and the artifact describe different things")
        verify_bytes(entry, files[entry.path], at="write")
        stored = store_file(conn, files[entry.path])
        if stored != entry.content_reference:
            raise ManifestIntegrityError(
                f"{entry.path} stored as {stored} but its manifest entry says "
                f"{entry.content_reference}: the reference was computed over different bytes")

    conn.execute(
        "INSERT INTO generated_artifact_file (artifact_id, path, sha256, byte_length, "
        "media_type, content_reference) VALUES " + ", ".join(
            ["(%s, %s, %s, %s, %s, %s)"] * len(manifest.entries)) +
        " ON CONFLICT (artifact_id, path) DO NOTHING",
        [value
         for entry in manifest.entries
         for value in (manifest.artifact_id, entry.path, entry.sha256, entry.byte_length,
                       entry.media_type, entry.content_reference)])


def fetch_file(conn, entry: ArtifactFileEntryV1, *, at: str = "retrieval") -> str:
    """The stored bytes for ``entry``, verified against it.

    Raises:
        ManifestIntegrityError: the reference is absent, or the bytes do not match the entry.
    """
    row = conn.execute(
        "SELECT content FROM generated_artifact_blob WHERE content_reference = %s",
        (entry.content_reference,)).fetchone()
    if row is None:
        raise ManifestIntegrityError(
            f"{entry.path} references {entry.content_reference}, which the store does not hold: "
            f"the manifest promises bytes nothing can produce")
    return verify_bytes(entry, row[0], at=at)
