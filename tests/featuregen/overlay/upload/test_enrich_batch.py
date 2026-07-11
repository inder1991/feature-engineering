from featuregen.overlay.upload import enrich
from featuregen.overlay.upload import enrich_config as cfg
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash


def test_mode_defaults_single_and_reads_env(monkeypatch):
    assert cfg.mode("concept") == "single"
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    assert cfg.mode("concept") == "batch"


def test_max_items_default_and_override(monkeypatch):
    assert cfg.max_items("concept") == 40
    assert cfg.max_items("definition") == 12
    assert cfg.max_items("domain") == 20
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "16")
    assert cfg.max_items("concept") == 16


def test_budget_defaults(monkeypatch):
    b = cfg.budget("definition")
    assert b.max_batch_attempts == 2 and b.max_single_fallback == 8 and b.min_split == 4
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "3")
    assert cfg.budget("definition").max_single_fallback == 3


def test_cache_is_version_scoped(db):
    row = CanonicalRow("deposits", "accounts", "balance", "numeric")
    h = content_hash(row)
    enrich._cache_put(db, "enrichment_concept", h, "monetary_stock", "vA")
    assert enrich._cache_get(db, "enrichment_concept", [h], "vA") == {h: "monetary_stock"}
    # A different cache_version does NOT see the vA entry -> forces recompute (spec C6).
    assert enrich._cache_get(db, "enrichment_concept", [h], "vB") == {}


def test_vocab_fingerprint_is_stable_and_short():
    fp = enrich._vocab_fingerprint()
    assert len(fp) == 12 and fp == enrich._vocab_fingerprint()
