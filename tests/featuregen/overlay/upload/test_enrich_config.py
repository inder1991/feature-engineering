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
        "concept": 20,
        # `definition` LOWERED 8 -> 4 (2026-08-06). Not a contamination re-tune: it is the only
        # stage whose OUTPUT can approach the response ceiling, because `MAX_DEFINITION_LEN` is
        # 32_000 chars (~8_000 output tokens). At 8 a full-length chunk needs ~64_000 tokens, which
        # is EXACTLY `llm._MAX_TOKENS_CEILING` — unservable even after the single truncation
        # escalation. See `test_enrich_output_bounds.py` for the asserted arithmetic.
        "definition": 4,
        "domain": 8, "synonyms": 8, "unit": 8,
        # `summary` runs over EVERY column, so it is the widest fan-out of any task — it sits at the
        # prose-task ceiling rather than above it, keeping cross-item contamination equally bounded.
        # It KEEPS 8: its accept bound is 1000 chars (~250 output tokens), so a full chunk is ~2_000
        # — it does not have `definition`'s response-ceiling problem.
        "summary": 8,
        "table_synth": 4}


def test_env_override_still_applies(monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "5")
    assert enrich_config.max_items("concept") == 5
