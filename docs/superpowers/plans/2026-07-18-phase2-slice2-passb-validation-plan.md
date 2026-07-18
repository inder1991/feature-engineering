# Phase-2 Slice 2 — Per-field Pass B Validation + Stale-Value Lifecycle + Durable Dispositions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Validate every Pass B table-synthesis field independently (complete grain checks, a versioned `table_role` vocabulary with aliases, `primary_entity` gated through the governed entity registry, `event_or_snapshot` normalization); when a field is dropped or abstained, **stale the prior value out of the graph** (not just the evidence); and record every field's disposition as durable, reviewer-visible state.

**Architecture:** All field validation happens in `make_ref_accept` (`table_synth.py`), which now (a) drops only the invalid field, never the whole synthesis, and (b) records a per-field disposition into a side-channel collector. Dropped/abstained advisory fields are producer-scope staled AND run through a new "touched-field resolver" that records a `STALED` decision and clears the `graph_node` display column + decision link. Dispositions persist as ingestion-run stage detail keyed by `(table, field)`.

**Tech Stack:** Python 3.11, psycopg3, PostgreSQL. Interpreter `.venv/bin/python`. Depends on Slice 1 (the version seam `prompt_version`/`schema_version` params).

## Global Constraints

- Branch off Slice 1's branch tip (Slice 2 depends on the Task-1 version seam). Confirm the base with the user.
- All subagent work on **Opus 4.8**.
- **Governance unchanged:** grain/availability stay PROPOSED-only governed facts; `table_role`/`primary_entity`/`event_or_snapshot` stay RECOMMENDATION-ceilinged advisory field evidence (`field_policies.py` `_TABLE_ADVISORY`). Nothing auto-verifies.
- **Reuse, don't reinvent:** `known_entities()` (`taxonomy/dimensions.py`) with the clear-on-miss pattern (`recognition.py`); `stale_source_evidence` (`field_evidence.py`) + the `_stale_absent_fields` shape (`ingest.py`); `_project_display`/`_DISPLAY_COLUMN`/`_DECISION_LINK_COLUMN`/`is_feature_eligible`/`_RETIRED_EVENTS` (`field_resolution.py`); `record_stage`/`StageRecorder` JSONB detail (`stage_report.py`); the version seam (`enrich_llm.audited_batch_call`).
- Disposition **status vocab**: `accepted`, `abstained`, `dropped_invalid`, `staled`. **Reason-code vocab**: `grain_col_not_in_table`, `grain_duplicate`, `grain_over_bound`, `role_off_vocab`, `entity_not_registered`, `basis_not_allowed`, `as_of_col_not_in_table`.
- **Verify line numbers before editing** — anchor on symbol names. (Current facts: `make_ref_accept` `table_synth.py:104`; the whole-synthesis grain reject at `:117-118`; the advisory `if v:` write at `:422-428`; `_ADVISORY_TABLE_FIELDS` `:328`; `_VALID_BASIS`; the two synth schemas `enrich_llm.py:269`/`:289` + summary `:307`; `resolve_and_project` processes only fields WITH active evidence.)
- Run `.venv/bin/python -m pytest <targets> -q` after each task; `.venv/bin/ruff check <files>`.

---

### Task 1: Complete per-field validation + versioned vocab + disposition collection + prompt v3

**Files:**
- Create: `src/featuregen/overlay/upload/table_vocab.py` (vocab + normalizers)
- Modify: `src/featuregen/overlay/upload/table_synth.py` (`make_ref_accept`, `make_summary_accept` role normalization, the three synth drivers pass `prompt_version=3`)
- Test: `tests/featuregen/overlay/upload/test_passb_field_validation.py`

**Interfaces:**
- Produces:
  - `table_vocab.MAX_GRAIN_COLS = 16`
  - `table_vocab.TABLE_ROLES = frozenset({"event_fact", "snapshot_fact", "dimension", "reference", "bridge"})`
  - `table_vocab.normalize_table_role(raw: str | None, *, event_or_snapshot: str | None) -> str | None` — aliases `dim`→`dimension`; `fact`→`event_fact` if `event_or_snapshot=="event"`, `snapshot_fact` if `"snapshot"`, else retained `fact`; `reference` kept; any other/unmapped → `None`.
  - `table_vocab.normalize_event_or_snapshot(raw) -> str | None` — `{"event","snapshot"}` else `None`.
  - `make_ref_accept(columns_by_table, *, dispositions: dict[tuple[str, str], dict] | None = None)` — validates each field independently, drops only the invalid one, and records a `{"status","reason"}` disposition per `(table, field)` into `dispositions`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_passb_field_validation.py
import json
from featuregen.overlay.upload.table_synth import make_ref_accept
from featuregen.overlay.upload.table_vocab import normalize_table_role, MAX_GRAIN_COLS


def _accept(cols):
    disp = {}
    return make_ref_accept({"t": set(cols)}, dispositions=disp), disp


def test_bad_grain_column_drops_only_grain_keeps_role_entity():
    accept, disp = _accept(["a", "b"])
    payload = json.dumps({"grain_columns": ["a", "ghost"], "table_role": "fact",
                          "primary_entity": "customer", "event_or_snapshot": "event"})
    value, reason = accept(payload, "t")
    out = json.loads(value)
    assert out["grain"] is None                                  # grain dropped
    assert out["table_role"] == "event_fact"                     # fact -> event_fact (kept)
    assert out["primary_entity"] == "customer"
    assert disp[("t", "grain")]["reason"] == "grain_col_not_in_table"


def test_grain_rejects_duplicates_and_over_bound():
    accept, disp = _accept(["a", "b"])
    dup = json.dumps({"grain_columns": ["a", "a"]})
    assert json.loads(accept(dup, "t")[0])["grain"] is None
    assert disp[("t", "grain")]["reason"] == "grain_duplicate"
    big_cols = [str(i) for i in range(MAX_GRAIN_COLS + 1)]
    accept2, disp2 = _accept(big_cols)
    over = json.dumps({"grain_columns": big_cols})
    assert json.loads(accept2(over, "t")[0])["grain"] is None
    assert disp2[("t", "grain")]["reason"] == "grain_over_bound"


def test_table_role_alias_and_off_vocab():
    assert normalize_table_role("dim", event_or_snapshot=None) == "dimension"
    assert normalize_table_role("fact", event_or_snapshot="snapshot") == "snapshot_fact"
    assert normalize_table_role("reference", event_or_snapshot=None) == "reference"
    assert normalize_table_role("wharrgarbl", event_or_snapshot=None) is None
    accept, disp = _accept(["a"])
    v = json.loads(accept(json.dumps({"grain_columns": [], "table_role": "wharrgarbl"}), "t")[0])
    assert v["table_role"] is None and disp[("t", "table_role")]["status"] == "dropped_invalid"


def test_primary_entity_gated_through_registry():
    accept, disp = _accept(["a"])
    v = json.loads(accept(json.dumps({"grain_columns": [], "primary_entity": "not_an_entity"}), "t")[0])
    assert v["primary_entity"] is None
    assert disp[("t", "primary_entity")]["reason"] == "entity_not_registered"


def test_event_or_snapshot_normalized():
    accept, _ = _accept(["a"])
    v = json.loads(accept(json.dumps({"grain_columns": [], "event_or_snapshot": "EVENTish"}), "t")[0])
    assert v["event_or_snapshot"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_field_validation.py -q` → FAIL.

- [ ] **Step 3: Implement `table_vocab.py`**

```python
# src/featuregen/overlay/upload/table_vocab.py
"""Versioned controlled vocabularies for Pass B table synthesis. table_role migrates from the legacy
open values (fact/dim/reference) to a controlled set via explicit aliases; an unmapped value is an
abstention (dropped), never active advisory evidence. primary_entity is gated by the governed
entity registry (known_entities). Enforced code-side; the schema keeps table_role a free string."""
from __future__ import annotations

MAX_GRAIN_COLS = 16   # matches the grain_columns maxItems in the synth schema

TABLE_ROLES = frozenset({"event_fact", "snapshot_fact", "dimension", "reference", "bridge"})
_ROLE_ALIASES = {"dim": "dimension", "reference": "reference"}


def normalize_event_or_snapshot(raw: str | None) -> str | None:
    return raw if raw in ("event", "snapshot") else None


def normalize_table_role(raw: str | None, *, event_or_snapshot: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if v in TABLE_ROLES:
        return v
    if v in _ROLE_ALIASES:
        return _ROLE_ALIASES[v]
    if v == "fact":
        eos = normalize_event_or_snapshot(event_or_snapshot)
        return "event_fact" if eos == "event" else "snapshot_fact" if eos == "snapshot" else "fact"
    return None
```
(`"fact"` with no event/snapshot signal is retained as `"fact"` — a legacy value the reviewer required not to silently drop; it is still a recognized advisory value.)

- [ ] **Step 4: Rewrite `make_ref_accept` for per-field validation + dispositions**

Replace the whole-synthesis grain reject with per-field validation. Key changes to the `accept` closure:
```python
def make_ref_accept(columns_by_table, *, dispositions=None):
    disp = dispositions if dispositions is not None else {}
    def accept(raw, ref):
        cols = columns_by_table.get(ref, set())
        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            return None, "unparseable"
        if not isinstance(s, dict):
            return None, "not_object"

        # --- grain: validate independently; drop grain only, never the whole synthesis ---
        raw_grain = [c for c in (s.get("grain_columns") or []) if isinstance(c, str)]
        grain = None
        if raw_grain:
            if len(raw_grain) != len(set(raw_grain)):
                disp[(ref, "grain")] = {"status": "dropped_invalid", "reason": "grain_duplicate"}
            elif len(raw_grain) > table_vocab.MAX_GRAIN_COLS:
                disp[(ref, "grain")] = {"status": "dropped_invalid", "reason": "grain_over_bound"}
            elif any(c not in cols for c in raw_grain):
                disp[(ref, "grain")] = {"status": "dropped_invalid", "reason": "grain_col_not_in_table"}
            else:
                grain = {"columns": raw_grain, "is_unique": True}
                disp[(ref, "grain")] = {"status": "accepted", "reason": None}
        else:
            disp[(ref, "grain")] = {"status": "abstained", "reason": None}

        # --- availability: unchanged decoupling, now with a disposition ---
        availability, as_of_col, as_of_basis = None, s.get("as_of_column"), s.get("as_of_basis")
        if as_of_col is not None:
            if as_of_col in cols and as_of_basis in _VALID_BASIS:
                availability = {"column": as_of_col, "basis": as_of_basis}
                disp[(ref, "availability_time")] = {"status": "accepted", "reason": None}
            else:
                reason = "basis_not_allowed" if as_of_col in cols else "as_of_col_not_in_table"
                disp[(ref, "availability_time")] = {"status": "dropped_invalid", "reason": reason}
                counters.incr("overlay.table_synth.availability.dropped_bad_as_of")

        # --- advisory: normalize/gate each; a dropped one is a disposition (staled later) ---
        eos = table_vocab.normalize_event_or_snapshot(s.get("event_or_snapshot"))
        role = table_vocab.normalize_table_role(s.get("table_role"), event_or_snapshot=eos)
        if s.get("table_role") and role is None:
            disp[(ref, "table_role")] = {"status": "dropped_invalid", "reason": "role_off_vocab"}
        ent = s.get("primary_entity")
        if isinstance(ent, str) and ent and ent not in known_entities():
            disp[(ref, "primary_entity")] = {"status": "dropped_invalid",
                                             "reason": "entity_not_registered"}
            ent = None

        out = {"grain": grain, "availability_time": availability,
               "table_role": role, "primary_entity": ent, "event_or_snapshot": eos}
        return json.dumps(out, sort_keys=True), ("valid" if (grain or availability) else "abstained")
    return accept
```
Add imports for `table_vocab` and `known_entities`. Apply `normalize_table_role`/`normalize_event_or_snapshot` in `make_summary_accept` too (its `entity_signals`/`event_or_snapshot` normalization). Bump `prompt_version=3` in the three synth drivers (`synthesize_tables`/`_run_synthesis`/`_synthesize_wide_tables` → `run_batched`), and update `_INSTRUCTION`/`_SUMMARY_INSTRUCTION`/`_SYNTH_WIDE_INSTRUCTION` to enumerate the controlled `table_role` values.

- [ ] **Step 5: Run to verify pass + Pass B regression**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_field_validation.py tests/featuregen/overlay/upload/test_passb_abstention.py tests/featuregen/overlay/upload/test_table_synth*.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/table_vocab.py src/featuregen/overlay/upload/table_synth.py tests/featuregen/overlay/upload/test_passb_field_validation.py
git commit -m "feat(passb): complete per-field validation — grain checks, versioned table_role vocab, entity-registry gating, dispositions, prompt v3"
```

---

### Task 2: Stale-value lifecycle — clear the projected graph column + decision link

**Files:**
- Modify: `src/featuregen/overlay/upload/field_resolution.py` (add `stale_and_clear_field(...)`)
- Modify: `src/featuregen/overlay/upload/table_synth.py` (`_propose_table_facts` — stale + clear dropped/absent advisory fields)
- Test: `tests/featuregen/overlay/upload/test_passb_stale_lifecycle.py`

**Interfaces:**
- Consumes: `stale_source_evidence`, `_DISPLAY_COLUMN`/`_DECISION_LINK_COLUMN`/`_project_display`/`_record`/`is_feature_eligible`, `_active_field_names`.
- Produces: `field_resolution.stale_and_clear_field(conn, *, source: str, logical_ref: str, field_name: str, now: datetime | None = None) -> None` — records a `STALED` field decision (display=None, load-bearing=None, no selected evidence) and calls `_project_display(display_value=None, decision_id=<staled>)` to clear the display column + repoint the link; rebuilds search if the field is search-doc-bearing.

- [ ] **Step 1: Write the failing test (assert the GRAPH, not just evidence)**

```python
# tests/featuregen/overlay/upload/test_passb_stale_lifecycle.py
# Round 1: Pass B proposes table_role='fact' -> resolve/project -> graph_node.table_role='event_fact'.
# Round 2: a re-upload where Pass B OMITS table_role -> the prior advisory value must be STALED and
# graph_node.table_role must be NULL, and is_feature_eligible(...) False.
def test_dropped_advisory_field_clears_graph_column_and_link(db):
    # helper: run _propose_table_facts + resolve_and_project for a synthesized table, then re-run
    # with the field absent; assert the graph column + decision link are cleared.
    ...
    assert row_after["table_role"] is None
    assert row_after["table_role_decision_id"] is None
```
(The implementer builds the two-round harness against the real `_propose_table_facts` + wherever `resolve_and_project` runs for the advisory refs — see Step 3.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_stale_lifecycle.py -q` → FAIL (stale leaves the graph value visible).

- [ ] **Step 3: Implement `stale_and_clear_field` + wire it**

In `field_resolution.py`, add:
```python
def stale_and_clear_field(conn, *, source, logical_ref, field_name, now=None):
    """The last active evidence for `field_name` was staled — record a STALED decision and clear the
    projected display column + decision link (resolve_and_project skips fields with no active
    evidence, so it never revisits a fully-staled field). Idempotent: a no-op if nothing is set."""
    now = now or datetime.now(UTC)
    decision_id = _record(conn, source=source, logical_ref=logical_ref, field_name=field_name,
                          display_value=None, load_bearing_value=None, selected_evidence=[],
                          event_type=FieldDecisionEventType.STALED, conflict_status="staled",
                          reason_codes=["evidence_staled"], now=now)
    _project_display(conn, source=source, logical_ref=logical_ref, field_name=field_name,
                     display_value=None, decision_id=decision_id)
```
(You must extend `_record` to accept an `event_type`/`display_value`/`load_bearing_value`/`selected_evidence`/`conflict_status`/`reason_codes` override — today it hardcodes `RESOLVED`. Keep the default behavior for the existing caller unchanged.)

In `table_synth.py` `_propose_table_facts`, after the advisory write loop, reconcile the dropped/absent advisory fields (producer-scope stale the LLM evidence, then clear the graph):
```python
        written = {f for f in _ADVISORY_TABLE_FIELDS if syn.get(f)}
        for field_name in _ADVISORY_TABLE_FIELDS:
            if field_name in written:
                continue
            n = stale_source_evidence(conn, logical_ref=logical_ref, field_name=field_name,
                                      producer=EvidenceProducer.LLM, keep_input_hash=_STALE_ALL)
            if n and not _active_field_names_has(conn, logical_ref, field_name):
                stale_and_clear_field(conn, source=source, logical_ref=logical_ref,
                                      field_name=field_name)
```
Import `stale_and_clear_field`, `stale_source_evidence`, `_STALE_ALL` (or a local sentinel), and a helper to test "no remaining active evidence for this field" (a scoped `_active_field_names`). **Trace where `resolve_and_project` runs for the Pass B advisory refs** and ensure this staling runs in the same transaction/order so the clear isn't re-projected away.

- [ ] **Step 4: Run to verify pass + regression**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_stale_lifecycle.py tests/featuregen/overlay/upload/test_field_resolution*.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/field_resolution.py src/featuregen/overlay/upload/table_synth.py tests/featuregen/overlay/upload/test_passb_stale_lifecycle.py
git commit -m "fix(passb): stale-value lifecycle — a dropped advisory field clears its graph column + decision link (STALED)"
```

---

### Task 3: Durable per-field dispositions as ingestion-run stage detail

**Files:**
- Modify: `src/featuregen/overlay/upload/table_synth.py` (`synthesize_tables` threads the `dispositions` collector out)
- Modify: `src/featuregen/overlay/upload/ingest.py` (Pass B block — pass the collector, fold it into the `pass_b` stage detail keyed by `table.field`)
- Test: `tests/featuregen/overlay/upload/test_passb_dispositions.py`

**Interfaces:**
- Consumes: `make_ref_accept(..., dispositions=…)` (Task 1), `record_stage(..., detail=…)` (`stage_report.py`).
- Produces: `synthesize_tables(..., dispositions: dict | None = None)` populates the collector; the `pass_b` stage `detail` gains `{"dispositions": {"<table>.<field>": {"status","reason"}, ...}}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_passb_dispositions.py
# Ingest a fixture where Pass B returns an off-vocab table_role and a ghost grain column; assert the
# ingestion_run_stage 'pass_b' detail carries dispositions with the right status + reason vocab.
def test_dispositions_persist_in_stage_detail(db, synthetic_ftr_upload):
    ...
    detail = _pass_b_stage_detail(db, run_id)
    assert detail["dispositions"]["txn.grain"]["reason"] in (
        "grain_col_not_in_table", "grain_duplicate", "grain_over_bound", None)
    assert detail["dispositions"]["txn.table_role"]["status"] in ("accepted", "dropped_invalid",
                                                                  "abstained", "staled")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_dispositions.py -q` → FAIL.

- [ ] **Step 3: Thread the collector into the stage detail**

`synthesize_tables` creates/accepts a `dispositions` dict and threads it into `make_ref_accept` (via `_run_synthesis`/`_synthesize_wide_tables`). Return it (or accept a passed-in collector). In `ingest.py`, create the collector, pass it to `synthesize_tables`, and merge it into the `pass_b` stage `detail` as `{"dispositions": {f"{t}.{field}": v for (t, field), v in disp.items()}}` (JSON-safe keys). Keep the existing `_enrichment_outcome` detail keys; add `dispositions` alongside.

- [ ] **Step 4: Run to verify pass + regression**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_dispositions.py tests/featuregen/overlay/upload/test_stage_wiring.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/table_synth.py src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_passb_dispositions.py
git commit -m "feat(passb): durable per-field dispositions in the pass_b stage detail (status + reason vocab)"
```

---

### Task 4: Integration — validation + staling + dispositions on the synthetic fixture

**Files:**
- Create/extend: `tests/featuregen/overlay/upload/test_slice2_acceptance.py`

- [ ] **Step 1: Write the acceptance assertions**

On the Phase-1 synthetic FTR fixture with a scripted FakeLLM: (a) a synthesis with a ghost grain column keeps its valid `table_role`/`primary_entity` (grain dropped only); (b) an off-vocab role and a non-registry entity are dropped with the right reason codes; (c) a second upload that omits a previously-proposed `table_role` clears `graph_node.table_role` + `table_role_decision_id` and `is_feature_eligible` is False; (d) the `pass_b` stage detail carries the dispositions; (e) `dim`/`fact`/`reference` all still accepted (via aliases).

- [ ] **Step 2: Run + full suite**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_slice2_acceptance.py -q` then `.venv/bin/python -m pytest -q 2>&1 | tail -8` (green).

- [ ] **Step 3: Commit**

```bash
git add tests/featuregen/overlay/upload/test_slice2_acceptance.py
git commit -m "test(passb): slice-2 integration — per-field validation, graph-clearing stale lifecycle, durable dispositions, vocab aliases"
```

---

## Self-Review

**Spec coverage:** complete grain checks + role vocab + entity gating + event normalization → Task 1; the stale-value lifecycle that clears the graph column + link → Task 2; durable dispositions with the defined vocab → Task 3; integration incl. re-upload staling + alias compatibility → Task 4. Prompt v3 via the version seam → Task 1.
**Placeholder scan:** Task 2 Step 1/Step 3 and Task 3 Step 3 require the implementer to trace the `resolve_and_project` call site for advisory refs and build the two-round harness — flagged explicitly with the exact graph assertions to satisfy (`table_role` NULL, `table_role_decision_id` NULL, `is_feature_eligible` False). The `_record` override is spelled out. All other code steps carry code.
**Type consistency:** `make_ref_accept(..., dispositions=…)` (Task 1) is consumed by `synthesize_tables(..., dispositions=…)` (Task 3); `stale_and_clear_field` (Task 2) is called from `_propose_table_facts` (Task 2) and exercised in Task 4; the status/reason vocab is identical across Tasks 1, 3, 4.
**Cross-slice:** the `prompt_version`/`schema_version` seam is Slice 1 Task 1 — Slice 2 must be branched on top of it; Slice 2 bumps `prompt_version` to 3 (schema stays at Slice 1's v2 — the vocab is enforced code-side, not in the schema).
