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
    # The FEATURE switch (OVERLAY_TABLE_SYNTH) is the ONLY gate on Pass B. The config namespace also
    # answers mode("table_synth"), but synthesize_tables intentionally NEVER consults it — Pass B is
    # BATCH-ONLY (a ref_aware task has no single-call seam; see the synthesize_tables docstring).
    # So the generic "single" default below must NOT be read as "feature off", nor as Pass B ever
    # taking a single-call path; this test pins the config namespace answering without error.
    from featuregen.overlay.upload.ingest import table_synth_enabled
    monkeypatch.delenv("OVERLAY_TABLE_SYNTH", raising=False)
    monkeypatch.delenv("OVERLAY_ENRICH_TABLE_SYNTH_MODE", raising=False)
    assert table_synth_enabled() is False            # FEATURE kill switch default OFF
    assert mode("table_synth") == "single"           # generic config default; NOT consulted by Pass B
    assert isinstance(max_items("table_synth"), int)
    assert isinstance(max_input_tokens("table_synth"), int)
