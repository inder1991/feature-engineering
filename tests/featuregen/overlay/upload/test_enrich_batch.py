from featuregen.overlay.upload import enrich_config as cfg


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
