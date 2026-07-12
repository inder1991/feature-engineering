from featuregen.overlay.upload.enrich_config import max_input_tokens, max_items, mode
from featuregen.overlay.upload.enrich_llm import _SCHEMAS


def test_batch_and_single_schemas_registered():
    batch = _SCHEMAS[("overlay_table_synth_batch", 1)]
    item = batch["properties"]["results"]["items"]
    props = item["properties"]
    assert item["required"] == ["ref", "synthesis"]
    syn = props["synthesis"]["properties"]
    assert set(syn) == {
        "grain_columns", "as_of_column", "as_of_basis",
        "primary_entity", "table_role", "event_or_snapshot",
    }
    assert ("overlay_table_synth", 1) in _SCHEMAS  # single-call fallback sibling


def test_two_independent_switches(monkeypatch):
    # The FEATURE switch (OVERLAY_TABLE_SYNTH) and the BATCH MODE (OVERLAY_ENRICH_TABLE_SYNTH_MODE)
    # are ORTHOGONAL. Feature-off means Pass B never runs; mode only chooses batch-vs-single WHEN it
    # runs. Setting mode=single must NOT be read as "feature off".
    from featuregen.overlay.upload.ingest import table_synth_enabled
    monkeypatch.delenv("OVERLAY_TABLE_SYNTH", raising=False)
    monkeypatch.delenv("OVERLAY_ENRICH_TABLE_SYNTH_MODE", raising=False)
    assert table_synth_enabled() is False            # FEATURE kill switch default OFF
    assert mode("table_synth") == "single"           # batch mode default single (only matters if on)
    assert isinstance(max_items("table_synth"), int)
    assert isinstance(max_input_tokens("table_synth"), int)
