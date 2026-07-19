"""Delivery B Task 5 (Slice-2 F17) — key-gated live canary for the Pass B v3/v2 contract.

The existing live canary (test_anthropic_live_canary) proves the PROJECTED wire schemas are ones the
real Anthropic structured-output API accepts, but it hand-builds its LLMRequests and stops at
`reg.validate` — it never drives the REAL Pass B call surface, so a live regression in the Slice-2
contract (PROMPT v3 `overlay_table_synth_v3` enumerating the code-side `table_vocab` role
vocabulary, over the canonical v2 `overlay_table_synth_batch` schema, with per-field validation +
TOTAL dispositions in `make_ref_accept`) would only surface on a production ingest while CI and the
hermetic Pass B suite stay green.

THIS canary drives the exact production surface against a real model: `CanonicalRow`s ->
`build_table_views` -> `assemble_table_items` -> `synthesize_tables` (narrow fast path — ONE
`run_batched` synthesis batch: task "table_synth", prompt `overlay_table_synth_v3` v3, schema
`overlay_table_synth_batch` v2), whose ref-aware `make_ref_accept` — the SAME validator the ingest
path runs, INSIDE the harness via `validate_batch_results(..., ref_aware=True)` — judges the live
response. Nothing is re-implemented: a synthesis only appears in the returned dict because the real
validator resolved it. When a key is present it asserts:

  (a) the representative table RESOLVES — per-field salvage means one invalid/off-vocab field
      (e.g. a live off-vocab table_role) can only drop THAT FIELD, never whole-reject ([F1]);
  (b) the resolved synthesis is the exact five-key per-field shape the ingest consumer
      (`_propose_table_facts`) reads, every value code-side-vocab-valid: grain columns/as-of column
      really on the table, basis in the lag-free `_VALID_BASIS`, role in `CANONICAL_TABLE_ROLES`,
      event_or_snapshot in {event, snapshot}, entity in `known_entities()`;
  (c) the disposition record set is TOTAL over `DISPOSITION_FIELDS` with the closed status
      vocabulary and `prior_value_staled=False` ([F12] — staling flips belong to the propose
      pass);
  (d) the REGISTERED canonical v2 schema keeps table_role/as_of_basis/event_or_snapshot BOUNDED
      STRINGS, never enums ([F1] — `reg.validate` runs over the whole envelope BEFORE the
      per-field
      accept, so a schema enum would whole-reject a synthesis over one off-vocab value);
  (e) the immutable llm_call audit rows THIS run wrote are stamped overlay_table_synth_v3 /
      prompt_version 3 / overlay_table_synth_batch v2 — the run really egressed under the Slice-2
      contract, never a silent fallback to the v1 generation.

Run it (needs a live key; skips cleanly without one — the only behaviour default CI / this env
sees; `-s` prints the live synthesis + dispositions for the manual eval):

    ANTHROPIC_API_KEY=... .venv/bin/python -m pytest -m eval \
        tests/eval/test_passb_table_synth_live_canary.py -q -s

`anthropic` is imported lazily inside the Claude adapter (only when a call is actually made), so
every module-level import here resolves without the SDK and collection never errors on the skip
path.
"""
from __future__ import annotations

import json
import os

import pytest

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_view import build_table_views
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.enrich_llm import register_enrichment_schemas
from featuregen.overlay.upload.table_synth import (
    _VALID_BASIS,
    DISPOSITION_FIELDS,
    assemble_table_items,
    synthesize_tables,
)
from featuregen.overlay.upload.table_vocab import CANONICAL_TABLE_ROLES
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

pytestmark = pytest.mark.eval

# A small representative TECHNICAL table (no glossary sidecar; dual-type profiles with
# operational_type set and declared_type blank), mirroring the hermetic Pass B fixtures. Lowercase
# names on purpose: the ingest caller keys `columns_by_table` by `r.table.strip().lower()` and the
# views by the same normalization, so the canary keys match the production keying exactly.
_TABLE = "transactions"
_COLUMNS = ("txn_id", "account_id", "amount", "posted_at")
_SYNTH_KEYS = {"grain", "availability_time", "table_role", "primary_entity", "event_or_snapshot"}


def _rows() -> list[CanonicalRow]:
    def row(column: str, type_: str) -> CanonicalRow:
        # Field order/names mirror overlay/upload/canonical.py::CanonicalRow (source is required).
        return CanonicalRow(source="canary", table=_TABLE, column=column, type=type_,
                            definition="", sensitivity="", is_grain=False, as_of=False,
                            as_of_basis="", cardinality="", additivity="", unit="", currency="",
                            entity="", joins_to="")
    return [row("txn_id", "string"), row("account_id", "string"),
            row("amount", "numeric"), row("posted_at", "timestamp")]


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="live provider required")
def test_passb_table_synth_live_canary(db, monkeypatch) -> None:
    # Wire the REAL adapter; FEATUREGEN_LLM_PROVIDER=anthropic also makes `_generation_settings`
    # pin the true model/max_tokens/thinking/effort the live call requests (never model "test").
    monkeypatch.setenv("FEATUREGEN_LLM_PROVIDER", "anthropic")
    register_enrichment_schemas(db)  # idempotent; registers the v2 rows + projection check

    # (d) [F1] on the REGISTERED canonical v2 schema: the closed vocabularies live in the PROMPT and
    # in `make_ref_accept`, NEVER as schema enums — an enum here would make `reg.validate` (which
    # runs over the whole envelope before the ref-aware accept) whole-reject a live synthesis over
    # one off-vocab value, destroying the per-field salvage this canary asserts in (a).
    reg = DocumentSchemaRegistry(db)
    schema = reg.schema_for("overlay_table_synth_batch", 2)
    assert schema is not None, "overlay_table_synth_batch v2 not registered"
    synth_props = schema["properties"]["results"]["items"]["properties"]["synthesis"]["properties"]
    for field in ("table_role", "as_of_basis", "event_or_snapshot"):
        assert "enum" not in synth_props[field], f"{field} must stay a bounded string ([F1])"
        assert synth_props[field]["type"] == ["string", "null"]
        assert "maxLength" in synth_props[field]

    # The REAL input assembly (Task-3 views -> Task-4 assembler), exactly as ingest builds it for a
    # technical upload: glossary/bindings/domains None, Pass-A concept joined by content_hash.
    rows = _rows()
    views = build_table_views(rows, glossary=None, bindings=None,
                              concepts={content_hash(rows[2]): "monetary_amount"},
                              definitions={}, domains=None)
    items = assemble_table_items(views)
    assert [it.ref for it in items] == [_TABLE]
    cols = {_TABLE: set(_COLUMNS)}

    before = {r[0] for r in db.execute(
        "SELECT llm_call_ref FROM llm_call WHERE task = 'table_synth'").fetchall()}

    # The REAL Pass B call: narrow fast path -> ONE governed synthesis batch under prompt v3 /
    # schema v2; `make_ref_accept` validates the live response inside `run_batched`.
    dispositions: list[dict] = []
    client = build_claude_llm(ClaudeConfig.from_env())
    syntheses = synthesize_tables(db, client, items, columns_by_table=cols,
                                  actor=None,  # audited_batch_call falls back to _ENRICH_ACTOR
                                  dispositions=dispositions)

    # Manual-eval record (visible with -s), like the other live canaries.
    print(json.dumps({"syntheses": syntheses, "dispositions": dispositions},
                     indent=2, sort_keys=True))

    # (a) The table RESOLVES. Per-field salvage ([F1]): an off-vocab role / ghost grain column in
    # the live response drops that field with its own disposition — only unparseable / non-object
    # raw (or a provider failure) whole-rejects, and THAT is the regression this canary exists
    # to catch.
    assert _TABLE in syntheses, (
        f"live Pass B synthesis did not resolve {_TABLE!r} — whole-item rejection or provider "
        f"failure under the v3/v2 contract; dispositions: {dispositions}")
    syn = syntheses[_TABLE]

    # (b) The exact per-field shape `_propose_table_facts` consumes, every value already through
    # the code-side vocab (nothing off-vocab may leak past `make_ref_accept`).
    assert set(syn) == _SYNTH_KEYS
    if syn["grain"] is not None:
        assert set(syn["grain"]) == {"columns", "is_unique"}
        assert syn["grain"]["is_unique"] is True  # the proposed CLAIM (human-gated later)
        assert syn["grain"]["columns"] and set(syn["grain"]["columns"]) <= set(_COLUMNS)
    if syn["availability_time"] is not None:
        assert set(syn["availability_time"]) == {"column", "basis"}
        assert syn["availability_time"]["column"] in _COLUMNS
        assert syn["availability_time"]["basis"] in _VALID_BASIS
    assert syn["table_role"] is None or syn["table_role"] in CANONICAL_TABLE_ROLES
    assert syn["event_or_snapshot"] in (None, "event", "snapshot")
    assert syn["primary_entity"] is None or syn["primary_entity"] in known_entities()

    # (c) [F12] TOTAL dispositions: exactly one record per field, closed status vocabulary, and
    # prior_value_staled untouched (the staling flips belong to `_propose_table_facts`, not here).
    recs = [d for d in dispositions if d["table"] == _TABLE]
    assert {d["field"] for d in recs} == set(DISPOSITION_FIELDS)
    assert len(recs) == len(DISPOSITION_FIELDS)
    assert all(d["status"] in {"accepted", "abstained", "dropped_invalid"} for d in recs)
    assert all(d["prior_value_staled"] is False for d in recs)

    # (e) The immutable llm_call rows THIS run wrote carry the Slice-2 stamp — the request really
    # egressed as prompt overlay_table_synth_v3 v3 over schema overlay_table_synth_batch v2.
    new = [r for r in db.execute(
        "SELECT llm_call_ref, prompt_id, prompt_version, output_schema_id, output_schema_version "
        "FROM llm_call WHERE task = 'table_synth'").fetchall() if r[0] not in before]
    assert new, "the live Pass B run wrote no immutable llm_call audit record"
    for _ref, prompt_id, prompt_version, schema_id, schema_version in new:
        assert (prompt_id, prompt_version, schema_id, schema_version) == (
            "overlay_table_synth_v3", 3, "overlay_table_synth_batch", 2)
