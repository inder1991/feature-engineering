"""C-D4 — the external artifact manifest and its Postgres content store.

The gate is *"the manifest type and the Postgres store are frozen; `GENERATED.lock` and `read_lock`
untouched"*, plus *"a mismatched digest is neither served nor executed"*. The untouched half is
tested against the real `read_lock`, because the risk is not theoretical: `run_l0` calls `_lock_of`
as its first statement inside the chain's transaction and does not catch, so an extended lock would
abort the compile.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.artifact_manifest import (
    ARTIFACT_MEDIA_TYPE,
    ArtifactFileEntryV1,
    ArtifactManifestV1,
    ManifestIntegrityError,
    manifest_for,
    verify_bytes,
)
from featuregen.materialize.artifact_store import (
    content_reference_for,
    fetch_file,
    store_file,
    store_manifest,
)
from featuregen.materialize.identity import GENERATED_LOCK_FILENAME, read_lock

FILES = {
    "README.md": "# account_daily\n",
    "conf/base/catalog.yml": "txns:\n  type: spark.SparkDataSet\n",
    GENERATED_LOCK_FILENAME: '{"compilation": {}, "generated_project_hash": "x"}',
}


def _manifest(files=None) -> ArtifactManifestV1:
    return manifest_for("art-1", files if files is not None else FILES,
                        content_reference=lambda path: content_reference_for(
                            (files if files is not None else FILES)[path]))


# ══ GENERATED.lock and read_lock are UNTOUCHED ═══════════════════════════════════════════════════
def test_THE_MANIFEST_EXCLUDES_THE_LOCK():
    """The lock's content is decided by this manifest's own existence, so a pointer to its bytes
    would point at something not yet decided."""
    assert [e.path for e in _manifest().entries] == ["README.md", "conf/base/catalog.yml"]
    assert _manifest().entry_for(GENERATED_LOCK_FILENAME) is None


def test_a_lock_entry_cannot_even_be_CONSTRUCTED():
    with pytest.raises(ValueError, match="cannot be a manifest entry"):
        ArtifactFileEntryV1(path=GENERATED_LOCK_FILENAME, sha256="a" * 64, byte_length=1,
                            media_type=ARTIFACT_MEDIA_TYPE, content_reference="sha256:x")


def test_READ_LOCK_STILL_REFUSES_A_THIRD_KEY():
    """Why the manifest is external at all. `read_lock` enforces a strict two-key top level, and
    `run_l0` calls it inside the chain's transaction without catching — an extended lock aborts the
    compile rather than producing a finding."""
    import json

    valid = json.dumps({
        # paired one per feature, as `CompilationIdentity` requires
        "compilation": {"formula_content_hashes": ["a" * 64], "ir_hashes": ["b" * 64],
                        "materialization_contract_hash": "c" * 64,
                        "group_plan_hash": "d" * 64},
        "generated_project_hash": "e" * 64})
    read_lock(valid)                                            # the shape it accepts today

    extended = json.loads(valid)
    extended["artifact_manifest"] = {"artifact_id": "art-1"}
    with pytest.raises(ValueError, match="must hold exactly a rendered identity"):
        read_lock(json.dumps(extended))


def test_the_manifest_module_does_not_import_or_touch_read_lock():
    import inspect

    from featuregen.materialize import artifact_manifest

    source = inspect.getsource(artifact_manifest)
    assert "read_lock" not in source.split('"""')[2], "only the docstring may mention it"


# ══ the entry carries all six facts ══════════════════════════════════════════════════════════════
def test_every_entry_carries_the_six_required_facts():
    entry = _manifest().entry_for("README.md")
    assert entry.path == "README.md"
    assert len(entry.sha256) == 64
    assert entry.byte_length == len(FILES["README.md"].encode("utf-8"))
    assert entry.media_type == ARTIFACT_MEDIA_TYPE
    assert entry.content_reference.startswith("sha256:")
    assert _manifest().artifact_id == "art-1"


def test_a_media_type_is_RECORDED_not_assumed():
    """"Assume UTF-8" is how a binary artifact later becomes mojibake nobody notices."""
    assert "charset=utf-8" in ARTIFACT_MEDIA_TYPE
    assert "media_type" in _manifest().entries[0].identity_payload()


@pytest.mark.parametrize("bad", ["", "A" * 64, "abc", "g" * 64])
def test_a_malformed_digest_is_refused(bad):
    with pytest.raises(ValueError, match="64-character lower-case hex"):
        ArtifactFileEntryV1(path="a.txt", sha256=bad, byte_length=1,
                            media_type=ARTIFACT_MEDIA_TYPE, content_reference="sha256:x")


def test_an_entry_with_no_content_reference_is_refused():
    """A catalogue rather than a manifest: it would record that a file exists without recording how
    to fetch it back."""
    with pytest.raises(ValueError, match="catalogue rather than a manifest"):
        ArtifactFileEntryV1(path="a.txt", sha256="a" * 64, byte_length=1,
                            media_type=ARTIFACT_MEDIA_TYPE, content_reference=" ")


def test_an_empty_manifest_is_refused():
    """It would let an empty retrieval look like a complete one."""
    with pytest.raises(ValueError, match="describes no artifact"):
        ArtifactManifestV1(artifact_id="art-1", entries=())


def test_a_duplicate_path_is_refused():
    entry = ArtifactFileEntryV1(path="a.txt", sha256="a" * 64, byte_length=1,
                               media_type=ARTIFACT_MEDIA_TYPE, content_reference="sha256:x")
    with pytest.raises(ValueError, match="two answers about what its bytes are"):
        ArtifactManifestV1(artifact_id="art-1", entries=(entry, entry))


# ══ verified on write, retrieval and execution ═══════════════════════════════════════════════════
def test_A_MISMATCHED_DIGEST_IS_NEITHER_SERVED_NOR_EXECUTED(db):
    """The plan's S7 gate."""
    manifest = _manifest()
    store_manifest(db, manifest, FILES)
    entry = manifest.entry_for("README.md")

    assert fetch_file(db, entry) == FILES["README.md"]          # served when it matches

    substituted = ArtifactFileEntryV1(
        path=entry.path, sha256="b" * 64, byte_length=entry.byte_length,
        media_type=entry.media_type, content_reference=entry.content_reference)
    with pytest.raises(ManifestIntegrityError, match="at retrieval"):
        fetch_file(db, substituted)
    with pytest.raises(ManifestIntegrityError, match="at execution"):
        fetch_file(db, substituted, at="execution")


@pytest.mark.parametrize("at", ["write", "retrieval", "execution"])
def test_the_verification_point_is_NAMED(at):
    """The three failures mean different things: at write the renderer disagrees with itself, at
    retrieval the store returned something else, at execution the bytes changed after fetching."""
    entry = _manifest().entry_for("README.md")
    with pytest.raises(ManifestIntegrityError, match=f"at {at}"):
        verify_bytes(entry, "different bytes entirely", at=at)


def test_a_LENGTH_mismatch_is_caught_too():
    entry = _manifest().entry_for("README.md")
    longer = ArtifactFileEntryV1(
        path=entry.path, sha256=entry.sha256, byte_length=entry.byte_length + 10,
        media_type=entry.media_type, content_reference=entry.content_reference)
    with pytest.raises(ManifestIntegrityError, match="length check"):
        verify_bytes(longer, FILES["README.md"], at="write")


def test_storing_a_manifest_VERIFIES_BEFORE_WRITING(db):
    """Catches the renderer disagreeing with itself — a manifest built from one dict and files
    taken from another. The only point where that mistake is still recoverable."""
    manifest = _manifest()
    with pytest.raises(ManifestIntegrityError, match="at write"):
        store_manifest(db, manifest, {**FILES, "README.md": "# something else\n"})


def test_a_manifest_naming_a_file_the_artifact_lacks_refuses(db):
    manifest = _manifest()
    with pytest.raises(ManifestIntegrityError, match="describe different things"):
        store_manifest(db, manifest, {"README.md": FILES["README.md"]})


def test_a_reference_the_store_does_not_hold_refuses(db):
    entry = _manifest().entry_for("README.md")
    missing = ArtifactFileEntryV1(
        path=entry.path, sha256=entry.sha256, byte_length=entry.byte_length,
        media_type=entry.media_type, content_reference="sha256:" + "0" * 64)
    with pytest.raises(ManifestIntegrityError, match="promises bytes nothing can produce"):
        fetch_file(db, missing)


# ══ the store is content-addressed and write-once ════════════════════════════════════════════════
def test_IDENTICAL_BYTES_SHARE_ONE_ROW(db):
    """A re-render of unchanged bytes stores nothing new."""
    first = store_file(db, "identical\n")
    second = store_file(db, "identical\n")
    assert first == second
    rows = db.execute(
        "SELECT count(*) FROM generated_artifact_blob WHERE content_reference = %s",
        (first,)).fetchone()
    assert rows[0] == 1


def test_the_blob_is_WRITE_ONCE(db):
    """It is content-addressed, so an UPDATE would make the reference a lie about its own bytes."""
    import psycopg

    reference = store_file(db, "immutable\n")
    with pytest.raises(psycopg.errors.RaiseException, match="content-addressed and write-once"):
        db.execute("UPDATE generated_artifact_blob SET content = %s WHERE content_reference = %s",
                   ("rewritten", reference))


def test_the_manifest_row_is_WRITE_ONCE(db):
    """A row that can be updated is one that can be brought into agreement with substituted bytes
    after the fact, which would make the verification ceremonial."""
    import psycopg

    store_manifest(db, _manifest(), FILES)
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"):
        db.execute("UPDATE generated_artifact_file SET sha256 = %s WHERE path = %s",
                   ("c" * 64, "README.md"))


def test_THE_DATABASE_ALSO_FORBIDS_A_LOCK_ROW(db):
    """The rule survives a caller that bypasses the writer."""
    import psycopg

    reference = store_file(db, "anything\n")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO generated_artifact_file (artifact_id, path, sha256, byte_length, "
            "media_type, content_reference) VALUES (%s, %s, %s, %s, %s, %s)",
            ("art-1", GENERATED_LOCK_FILENAME, "a" * 64, 1, ARTIFACT_MEDIA_TYPE, reference))


# ══ nothing is written into the project tree ═════════════════════════════════════════════════════
def test_NEITHER_MODULE_WRITES_TO_THE_FILESYSTEM():
    """The cluster gate hashes every file in the tree except the lock and `.pyc`, so a manifest
    sidecar or store receipt beside the source fails PROJECT_INTEGRITY."""
    import inspect

    from featuregen.materialize import artifact_manifest, artifact_store

    for module in (artifact_manifest, artifact_store):
        source = inspect.getsource(module)
        for writer in ("open(", "write_text", "write_bytes", "Path(", "mkdir", "os.replace"):
            assert writer not in source, f"{module.__name__}: {writer}"
