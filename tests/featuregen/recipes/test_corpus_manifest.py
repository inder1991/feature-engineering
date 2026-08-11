"""BR-22 — the gold corpus publishes its version and content hash (no self-refresh)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

_BASE = Path(__file__).parent


def test_the_manifest_matches_the_corpus_exactly():
    manifest = json.loads((_BASE / "gold_corpus_manifest.json").read_text())
    files = sorted([*(_BASE / "fixtures").glob("*.json"), *(_BASE / "gold").glob("*.json")])
    digest = hashlib.sha256()
    for f in files:
        digest.update(f.name.encode())
        digest.update(f.read_bytes())
    assert manifest["corpus_version"] == "banking-gold-corpus@1"
    assert manifest["files"] == [f.name for f in files]
    assert manifest["content_sha256"] == digest.hexdigest(), (
        "the corpus changed without a reviewed manifest regeneration")
