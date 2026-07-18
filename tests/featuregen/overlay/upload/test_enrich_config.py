"""MF-8a — the batch ceilings are conservative isolation boundaries, not throughput maxima.

The old ceilings (40/12/20/8) were throughput picks with NO accuracy evidence: the only quality
gate drives a scripted FakeLLM that echoes the expected answer per column, so it measures the
harness, not the provider, and compares no batch sizes. Until a real-provider batch-size sweep
(tests/eval/test_batch_size_sweep.py) produces the evidence a higher ceiling would require, the
defaults stay small so cross-item contamination has less room. The env override
(``OVERLAY_ENRICH_BATCH_<T>_MAX_ITEMS``) still lets an operator raise a ceiling per task.
"""
from __future__ import annotations

from featuregen.overlay.upload import enrich_config


def test_conservative_default_ceilings():
    assert enrich_config._DEFAULT_MAX_ITEMS == {
        "concept": 20, "definition": 8, "domain": 8, "table_synth": 4}


def test_env_override_still_applies(monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "5")
    assert enrich_config.max_items("concept") == 5
