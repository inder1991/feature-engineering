# Phase-2 Slice 2 — Per-field Pass B Validation + Stale-Value Lifecycle + Durable Dispositions — Implementation Plan (rev. 2)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development, task-by-task. Steps use checkbox (`- [ ]`).
> **rev. 2** incorporates a 17-finding review; Slice-2 items `[F9]`–`[F17]` addressed inline.

**Goal:** Validate every Pass B field independently (normalized grain checks, a versioned+schema-enforced `table_role` vocabulary, `primary_entity` gated through `known_entities()`, normalized `event_or_snapshot`); when a field is dropped/abstained, **stale the prior value out of the graph**; and record a **total** per-field disposition set (all five fields for every evaluated table, plus table-level `not_evaluated`) as durable, structured, reviewer-visible state.

**Architecture:** Field validation lives in `make_ref_accept`; it drops only the invalid field and appends a per-field disposition record. Dropped/absent advisory fields are producer-scope staled AND cleared from `graph_node` via a `STALED` decision. Dispositions are a **list of records** (no delimited keys), and `staled` is a lifecycle flag (`prior_value_staled`), not a status.

**Tech Stack:** Python 3.11, psycopg3, PostgreSQL. Interpreter `.venv/bin/python`. **Depends on Slice 1** (the version seam; Slice 1 left Pass B at schema/prompt v2 — Slice 2 bumps to **v3**).

## Global Constraints

- Branch off Slice 1's tip. All subagent work on **Opus 4.8**.
- **Governance unchanged:** grain/availability stay PROPOSED governed facts; the three advisory fields stay RECOMMENDATION-ceilinged evidence; nothing auto-verifies.
- **Vocab consistency `[F9]`:** the schema enum lists the **accepted raw** values `{fact, dim, reference, event_fact, snapshot_fact, dimension, bridge}`; code normalizes aliases to a **canonical** set `{event_fact, snapshot_fact, dimension, reference, bridge, fact}` (legacy `fact` retained as canonical). `event_or_snapshot` and `primary_entity` are `strip().lower()`-normalized before matching.
- **Schema-first + code backup `[F10]`:** Pass B ships schema **v3** with a nullable-enum `table_role`; code-side normalization/gating remains as defense. `primary_entity` stays a free string (38-value enum is verbose) gated code-side through `known_entities()`.
- **Total dispositions `[F12]`:** every evaluated table produces a disposition for **all five** fields (`grain`, `availability_time`, `table_role`, `primary_entity`, `event_or_snapshot`); a table that never reached `make_ref_accept` (unresolved/failed) gets a table-level `not_evaluated`. `abstained` (evaluated, model gave nothing) ≠ `not_evaluated`.
- **Disposition shape `[F13,F15]`:** a **list** of `{"table","field","status","reason","prior_value_staled"}` records — never a `"table.field"` string key. `status ∈ {accepted, abstained, dropped_invalid, not_evaluated}`; `prior_value_staled` is a separate lifecycle bool set by `_propose_table_facts`. **Reason vocab:** `grain_invalid_shape, grain_duplicate, grain_over_bound, grain_col_not_in_table, role_off_vocab, entity_not_registered, basis_not_allowed, as_of_col_not_in_table`.
- **Stale contract `[F14]`:** a fully-staled field gets a `STALED` decision (`supersedes_event_id` = the prior decision); `_project_display(display_value=None, decision_id=<staled>)` clears the **display column** (NULL) and repoints the **link** to the STALED decision (an audit trail — the link is NOT NULL). Assert the display column is NULL, the latest decision is STALED, and no active LLM evidence remains — **not** `is_feature_eligible` (always False for ceiling fields), **not** link-IS-NULL.
- **Test commands:** run pytest directly (no `| tail` — it masks the exit code) `[F16]`; exact-match assertions from scripted FakeLLM, no `...`.
- **Verify line numbers** — anchor on symbols. (Current: `make_ref_accept:104` [grain whole-reject `:117-118`]; advisory `if v:` write `:422-428`; `_ADVISORY_TABLE_FIELDS:328`; schemas `enrich_llm.py:269/289/307`; `resolve_and_project` processes only fields WITH active evidence.)

---

### Task 1: Vocab module + schema v3 + complete validation + total dispositions + prompt/schema v3

**Files:** Create `table_vocab.py`; modify `table_synth.py` (`make_ref_accept`, `make_summary_accept`, synth drivers → `prompt_version=3, schema_version=3`), `enrich_llm.py` (`_SCHEMAS` v3 with the `table_role` enum, registered). Test `tests/featuregen/overlay/upload/test_passb_field_validation.py`.

**Interfaces:**
- `table_vocab.MAX_GRAIN_COLS = 16`
- `table_vocab.TABLE_ROLE_ENUM = ["fact","dim","reference","event_fact","snapshot_fact","dimension","bridge"]` (schema enum, accepted raw)
- `table_vocab.CANONICAL_TABLE_ROLES = frozenset({"event_fact","snapshot_fact","dimension","reference","bridge","fact"})`
- `table_vocab.normalize_table_role(raw, *, event_or_snapshot) -> str | None`
- `table_vocab.normalize_event_or_snapshot(raw) -> str | None`
- `make_ref_accept(columns_by_table, *, dispositions: list | None = None)` — appends `{"table","field","status","reason","prior_value_staled": False}` records for all five fields per call.

- [ ] **Step 1: Write the failing tests (exact, no placeholders)**

```python
# tests/featuregen/overlay/upload/test_passb_field_validation.py
import json
from featuregen.overlay.upload.table_synth import make_ref_accept
from featuregen.overlay.upload.table_vocab import (
    normalize_table_role, normalize_event_or_snapshot, MAX_GRAIN_COLS, CANONICAL_TABLE_ROLES)


def _accept(cols):
    disp = []
    return make_ref_accept({"t": set(cols)}, dispositions=disp), disp


def _find(disp, field):
    return next(d for d in disp if d["table"] == "t" and d["field"] == field)


def test_vocab_is_internally_consistent():
    assert normalize_table_role("fact", event_or_snapshot=None) in CANONICAL_TABLE_ROLES  # "fact"
    assert normalize_table_role("dim", event_or_snapshot=None) == "dimension"
    assert normalize_table_role("fact", event_or_snapshot="snapshot") == "snapshot_fact"
    assert normalize_table_role("FACT ", event_or_snapshot="event") == "event_fact"       # strip/lower
    assert normalize_table_role("nonsense", event_or_snapshot=None) is None
    assert normalize_event_or_snapshot(" Event ") == "event"                              # strip/lower


def test_grain_normalized_duplicate_and_invalid_shape():
    accept, disp = _accept(["Id", "amt"])
    # case-variant duplicate must be caught (normalized)
    assert json.loads(accept(json.dumps({"grain_columns": ["id", "ID"]}), "t")[0])["grain"] is None
    assert _find(disp, "grain")["reason"] == "grain_duplicate"
    # a non-string element is an invalid shape, NOT silently filtered
    accept2, disp2 = _accept(["id"])
    assert json.loads(accept2(json.dumps({"grain_columns": ["id", 7]}), "t")[0])["grain"] is None
    assert _find(disp2, "grain")["reason"] == "grain_invalid_shape"


def test_grain_maps_back_to_canonical_table_spelling():
    accept, disp = _accept(["CustomerId"])
    out = json.loads(accept(json.dumps({"grain_columns": ["customerid"]}), "t")[0])
    assert out["grain"]["columns"] == ["CustomerId"]        # canonical table spelling, not the input
    assert _find(disp, "grain")["status"] == "accepted"


def test_grain_over_bound():
    big = [str(i) for i in range(MAX_GRAIN_COLS + 1)]
    accept, disp = _accept(big)
    assert json.loads(accept(json.dumps({"grain_columns": big}), "t")[0])["grain"] is None
    assert _find(disp, "grain")["reason"] == "grain_over_bound"


def test_bad_grain_keeps_role_and_entity():
    accept, disp = _accept(["a"])
    out = json.loads(accept(json.dumps({"grain_columns": ["ghost"], "table_role": "fact",
                                        "primary_entity": "customer", "event_or_snapshot": "event"}),
                            "t")[0])
    assert out["grain"] is None and out["table_role"] == "event_fact"
    assert out["primary_entity"] == "customer"
    assert _find(disp, "grain")["reason"] == "grain_col_not_in_table"


def test_off_vocab_role_and_unregistered_entity_dropped():
    accept, disp = _accept(["a"])
    out = json.loads(accept(json.dumps({"grain_columns": [], "table_role": "wat",
                                        "primary_entity": "Customer"}), "t")[0])
    assert out["table_role"] is None and _find(disp, "table_role")["reason"] == "role_off_vocab"
    assert out["primary_entity"] == "customer"              # "Customer" normalized + registered
    accept2, disp2 = _accept(["a"])
    out2 = json.loads(accept2(json.dumps({"grain_columns": [], "primary_entity": "zzz"}), "t")[0])
    assert out2["primary_entity"] is None
    assert _find(disp2, "primary_entity")["reason"] == "entity_not_registered"


def test_dispositions_are_total_five_fields():
    accept, disp = _accept(["a"])
    accept(json.dumps({"grain_columns": []}), "t")
    fields = {d["field"] for d in disp}
    assert fields == {"grain", "availability_time", "table_role", "primary_entity",
                      "event_or_snapshot"}
    assert _find(disp, "table_role")["status"] == "abstained"   # absent advisory == abstained
```

- [ ] **Step 2: Run — expect FAIL** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_field_validation.py -q`

- [ ] **Step 3: Implement `table_vocab.py`**

```python
# src/featuregen/overlay/upload/table_vocab.py
from __future__ import annotations

MAX_GRAIN_COLS = 16
TABLE_ROLE_ENUM = ["fact", "dim", "reference", "event_fact", "snapshot_fact", "dimension", "bridge"]
CANONICAL_TABLE_ROLES = frozenset({"event_fact", "snapshot_fact", "dimension", "reference",
                                   "bridge", "fact"})
_ROLE_ALIASES = {"dim": "dimension"}


def normalize_event_or_snapshot(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    return v if v in ("event", "snapshot") else None


def normalize_table_role(raw: str | None, *, event_or_snapshot: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if v in CANONICAL_TABLE_ROLES:
        return v
    if v in _ROLE_ALIASES:
        return _ROLE_ALIASES[v]
    if v == "fact":                                        # unreachable (fact in CANONICAL) — kept explicit
        return "fact"
    if v in ("event_fact", "snapshot_fact"):
        return v
    return None
```
(Note: `"fact"` is canonical; the `event_or_snapshot`-driven split happens on the RAW alias path handled in `make_ref_accept`: when the model returns `"fact"` AND an `event_or_snapshot`, prefer `event_fact`/`snapshot_fact`. Implement that preference in `make_ref_accept` where both fields are in scope — see Step 4 — rather than passing `event_or_snapshot` deeper. The `event_or_snapshot` param remains for callers that want the split at normalize time.)

Refine: put the fact-split in `normalize_table_role` since it takes `event_or_snapshot`:
```python
    if v == "fact":
        return {"event": "event_fact", "snapshot": "snapshot_fact"}.get(
            normalize_event_or_snapshot(event_or_snapshot), "fact")
```
and drop the `"fact"` early-return in `CANONICAL_TABLE_ROLES` by checking aliases/fact BEFORE the canonical membership. Order: strip/lower → if `"fact"` split-or-retain → alias map → canonical membership → `event_fact`/`snapshot_fact` → None.

- [ ] **Step 4: Rewrite `make_ref_accept`** for normalized grain, normalized advisory, total dispositions:

```python
def make_ref_accept(columns_by_table, *, dispositions=None):
    disp = dispositions if dispositions is not None else []
    def _put(ref, field, status, reason=None):
        disp.append({"table": ref, "field": field, "status": status, "reason": reason,
                     "prior_value_staled": False})
    def accept(raw, ref):
        cols = columns_by_table.get(ref, set())
        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            return None, "unparseable"
        if not isinstance(s, dict):
            return None, "not_object"

        # grain
        rg = s.get("grain_columns")
        grain = None
        if rg in (None, []):
            _put(ref, "grain", "abstained")
        elif not isinstance(rg, list) or not all(isinstance(c, str) for c in rg):
            _put(ref, "grain", "dropped_invalid", "grain_invalid_shape")
        else:
            fold = [c.strip().lower() for c in rg]
            back = {c.lower(): c for c in cols}
            if len(fold) != len(set(fold)):
                _put(ref, "grain", "dropped_invalid", "grain_duplicate")
            elif len(rg) > table_vocab.MAX_GRAIN_COLS:
                _put(ref, "grain", "dropped_invalid", "grain_over_bound")
            elif any(f not in back for f in fold):
                _put(ref, "grain", "dropped_invalid", "grain_col_not_in_table")
            else:
                grain = {"columns": [back[f] for f in fold], "is_unique": True}
                _put(ref, "grain", "accepted")

        # availability
        availability, aoc, aob = None, s.get("as_of_column"), s.get("as_of_basis")
        if aoc is None:
            _put(ref, "availability_time", "abstained")
        elif aoc in cols and aob in _VALID_BASIS:
            availability = {"column": aoc, "basis": aob}
            _put(ref, "availability_time", "accepted")
        else:
            _put(ref, "availability_time", "dropped_invalid",
                 "basis_not_allowed" if aoc in cols else "as_of_col_not_in_table")

        # advisory (normalized/gated), each with a disposition
        eos = table_vocab.normalize_event_or_snapshot(s.get("event_or_snapshot"))
        _put(ref, "event_or_snapshot", "accepted" if eos else "abstained")
        rr = s.get("table_role")
        role = table_vocab.normalize_table_role(rr, event_or_snapshot=eos)
        if rr and role is None:
            _put(ref, "table_role", "dropped_invalid", "role_off_vocab")
        else:
            _put(ref, "table_role", "accepted" if role else "abstained")
        ent = s.get("primary_entity")
        ent = ent.strip().lower() if isinstance(ent, str) else None
        if ent and ent not in known_entities():
            _put(ref, "primary_entity", "dropped_invalid", "entity_not_registered"); ent = None
        else:
            _put(ref, "primary_entity", "accepted" if ent else "abstained")

        out = {"grain": grain, "availability_time": availability,
               "table_role": role, "primary_entity": ent, "event_or_snapshot": eos}
        return json.dumps(out, sort_keys=True), ("valid" if (grain or availability) else "abstained")
    return accept
```
Add `table_vocab` + `known_entities` imports. Apply the same normalization in `make_summary_accept`. Add the **v3** synth schemas (copy v2, set `table_role` to `{"type": ["string","null"], "enum": table_vocab.TABLE_ROLE_ENUM + [None]}`) + register; pass `prompt_version=3, schema_version=3` from the synth drivers; update `_INSTRUCTION`/`_SUMMARY_INSTRUCTION`/`_SYNTH_WIDE_INSTRUCTION` to enumerate the accepted `table_role` values.

- [ ] **Step 5: Run — expect PASS** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_field_validation.py tests/featuregen/overlay/upload/test_passb_abstention.py tests/featuregen/overlay/upload/test_table_synth*.py -q`
- [ ] **Step 6: Commit** `feat(passb): normalized per-field validation, consistent vocab, schema+prompt v3, total dispositions`

---

### Task 2: Stale-value lifecycle — clear the graph display + record a STALED decision

**Files:** Modify `field_resolution.py` (`stale_and_clear_field`, `_record` override for `event_type`/`supersedes_event_id`/None values), `table_synth.py` (`_propose_table_facts` — stale dropped/absent advisory fields + set `prior_value_staled`). Test `tests/featuregen/overlay/upload/test_passb_stale_lifecycle.py`.

**Interfaces:** `field_resolution.stale_and_clear_field(conn, *, source, logical_ref, field_name, now=None) -> None`; `_propose_table_facts(..., dispositions: list | None = None)` sets `prior_value_staled=True` on the matching `{table, field}` record when it stales one.

- [ ] **Step 1: Write the failing test (assert the graph + the decision, per `[F14]`)**

```python
# tests/featuregen/overlay/upload/test_passb_stale_lifecycle.py
# Round 1: propose table_role='fact' (+event) -> resolve/project -> graph_node.table_role='event_fact'.
# Round 2: re-propose with table_role ABSENT -> the field is staled and cleared.
def test_dropped_advisory_field_is_staled_and_cleared(db, passb_two_round_harness):
    graph, decisions, evidence = passb_two_round_harness(
        db, round1={"grain_columns": [], "table_role": "fact", "event_or_snapshot": "event"},
        round2={"grain_columns": []})            # table_role omitted in round 2
    assert graph["table_role"] is None                       # display column cleared
    assert decisions.latest("table_role").event_type == "STALED"
    assert decisions.latest("table_role").supersedes_event_id is not None
    assert evidence.active_llm("table_role") == []           # no active LLM evidence remains
```
(The harness runs `_propose_table_facts` + `resolve_and_project` for the table ref across two rounds and exposes the graph row, the field-decision log, and active evidence.)

- [ ] **Step 2: Run — expect FAIL** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_stale_lifecycle.py -q`

- [ ] **Step 3: Implement `stale_and_clear_field` + wire it**

Extend `_record` to accept `event_type=FieldDecisionEventType.RESOLVED`, `display_value` (may be None), `load_bearing_value` (may be None), `selected_evidence=()`, `conflict_status`, `reason_codes`, `supersedes_event_id=None` — keeping the existing caller's defaults unchanged. Add:
```python
def stale_and_clear_field(conn, *, source, logical_ref, field_name, now=None):
    now = now or datetime.now(UTC)
    prev = _current_decision_id(conn, source, logical_ref, field_name)   # read the graph link column
    decision_id = _record(conn, source=source, logical_ref=logical_ref, field_name=field_name,
                          display_value=None, load_bearing_value=None, selected_evidence=(),
                          event_type=FieldDecisionEventType.STALED, supersedes_event_id=prev,
                          conflict_status="staled", reason_codes=["evidence_staled"], now=now)
    _project_display(conn, source=source, logical_ref=logical_ref, field_name=field_name,
                     display_value=None, decision_id=decision_id)
```
Add `_current_decision_id` (SELECT the `*_decision_id` link from `graph_node` via `_graph_key`).

In `_propose_table_facts`, after the advisory write loop, reconcile absent/dropped advisory fields:
```python
        written = {f for f in _ADVISORY_TABLE_FIELDS if syn.get(f)}
        for field_name in _ADVISORY_TABLE_FIELDS:
            if field_name in written:
                continue
            n = stale_source_evidence(conn, logical_ref=logical_ref, field_name=field_name,
                                      producer=EvidenceProducer.LLM, keep_input_hash=_STALE_ALL)
            if n and not _active_field_names(conn, logical_ref) & {field_name}:
                stale_and_clear_field(conn, source=source, logical_ref=logical_ref,
                                      field_name=field_name)
                _mark_staled(dispositions, table, field_name)   # prior_value_staled = True
```
Add imports (`stale_and_clear_field`, `stale_source_evidence`, `_active_field_names`, `_STALE_ALL`) and `_mark_staled(disp, table, field)` (find/append the `{table, field}` record → `prior_value_staled=True`). **Trace** where `resolve_and_project` runs for the Pass B advisory refs and ensure this staling is in the same transaction so the clear is not re-projected away.

- [ ] **Step 4: Run — expect PASS** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_stale_lifecycle.py tests/featuregen/overlay/upload/test_field_resolution*.py -q`
- [ ] **Step 5: Commit** `fix(passb): stale lifecycle — STALED decision (supersedes) + display cleared; prior_value_staled on the disposition`

---

### Task 3: Durable, total dispositions in the `pass_b` stage detail

**Files:** Modify `table_synth.py` (`synthesize_tables(..., dispositions=…)` threads the collector; returns it), `ingest.py` (create the collector, add `not_evaluated` for unresolved tables, fold into `pass_b` stage detail). Test `tests/featuregen/overlay/upload/test_passb_dispositions.py`.

**Interfaces:** the `pass_b` stage `detail` gains `"dispositions": [ {"table","field","status","reason","prior_value_staled"}, ... ]` (a list, JSON-safe).

- [ ] **Step 1: Write the failing test (exact scripted result)**

```python
# tests/featuregen/overlay/upload/test_passb_dispositions.py
def test_dispositions_persist_total_in_stage_detail(db, synthetic_ftr_upload_scripted):
    # script FakeLLM to return, for table 'txn': grain_columns=['ghost'], table_role='wat'
    run_id = synthetic_ftr_upload_scripted(db, source="ftr_disp", synthesis={
        "txn": {"grain_columns": ["ghost"], "table_role": "wat", "grain_columns_valid": False}})
    detail = _pass_b_stage_detail(db, run_id)
    recs = {(d["table"], d["field"]): d for d in detail["dispositions"]}
    assert recs[("txn", "grain")]["reason"] == "grain_col_not_in_table"
    assert recs[("txn", "table_role")]["status"] == "dropped_invalid"
    assert recs[("txn", "table_role")]["reason"] == "role_off_vocab"
    # totality: all five fields present for the evaluated table
    assert {f for (t, f) in recs if t == "txn"} == {
        "grain", "availability_time", "table_role", "primary_entity", "event_or_snapshot"}
```

- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Thread + totalize** — `synthesize_tables` creates/accepts the `dispositions` list, threads it into `make_ref_accept` (via `_run_synthesis`/`_synthesize_wide_tables`) and into `_propose_table_facts`, and returns it. In `ingest.py`, after synthesis, add a `not_evaluated` record for every assembled table absent from `syntheses` (`[F12]`), then set `detail["dispositions"] = collector` alongside the existing `_enrichment_outcome` keys.
- [ ] **Step 4: Run — expect PASS** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_passb_dispositions.py tests/featuregen/overlay/upload/test_stage_wiring.py -q`
- [ ] **Step 5: Commit** `feat(passb): durable total per-field dispositions (list records + table-level not_evaluated) in pass_b stage detail`

---

### Task 4: Integration on the synthetic fixture

**Files:** Create `tests/featuregen/overlay/upload/test_slice2_acceptance.py`.

- [ ] **Step 1: Exact assertions** — on the synthetic FTR fixture: (a) a ghost grain column keeps a valid `table_role`/`primary_entity`; (b) `dim`/`fact`/`reference` all accepted via the vocab (canonical outputs `dimension`/`fact-or-event_fact`/`reference`); (c) an off-vocab role + non-registry entity dropped with the exact reason codes; (d) a second upload omitting a prior `table_role` → `graph_node.table_role` NULL, latest decision STALED, no active LLM evidence, and the disposition record `prior_value_staled=True`; (e) the `pass_b` stage detail carries total dispositions.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_slice2_acceptance.py -q`; then `.venv/bin/python -m pytest -q` (directly).
- [ ] **Step 3: Commit** `test(passb): slice-2 integration — normalized validation, graph-clearing stale, total dispositions, vocab aliases`

---

### Task 5: Real-provider canary for the v3 Pass B contract `[F17]`

**Files:** Modify `tests/eval/test_anthropic_live_canary.py`.

- [ ] **Step 1: Add** the `overlay_table_synth_batch` **v3** schema (the `table_role` nullable-enum) to the canary, registering v3 and validating a real response against the canonical v3 schema (proves the enum + nullable projects and the provider honors it). `skipif(not ANTHROPIC_API_KEY)`, `anthropic` imported in-body.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -m eval tests/eval/test_anthropic_live_canary.py -q` → SKIPPED without a key. A keyed run gates slice completion.
- [ ] **Step 3: Commit** `test(eval): live canary covers the Pass B v3 table_role enum schema`

---

## Self-Review

**Spec coverage:** consistent vocab + normalized validation + schema v3 `[F9,F10,F11]` → T1; total dispositions `[F12]` → T1+T3; disposition shape + staled-as-flag `[F13,F15]` → T1+T3; corrected stale contract `[F14]` → T2; provider canary `[F17]` → T5. All test steps are concrete `[F16]`.
**Placeholder scan:** no `...` in any test; harnesses (Task 2/3) are named with the exact assertions they must satisfy. Commands run pytest directly.
**Type consistency:** `make_ref_accept(..., dispositions=list)` (T1) ← `synthesize_tables(..., dispositions=…)` (T3) ← `_propose_table_facts(..., dispositions=…)` (T2, sets `prior_value_staled`); the reason/status vocab is identical across T1/T3/T4; `stale_and_clear_field` (T2) called from `_propose_table_facts` (T2), asserted in T4.
**Cross-slice:** depends on Slice 1's version seam; bumps Pass B **prompt** to v3 (the schema stays at Slice 1's v2 — see F1 below).

---

## rev. 3 — BINDING corrections (verified against code; override the tasks above)

Each was adversarially confirmed against the current source. Apply verbatim.

- **[F1] REVERSE the schema-enum decision — `table_role` stays a bounded STRING in the canonical schema.** The driver validates the response with `reg.validate(schema_id, version, output)`; a strict `table_role` enum on the canonical schema makes one off-vocab role fail the **whole** synthesis (losing grain too), destroying per-field salvage. Do **not** add a v3 schema with a `table_role` enum. Enforce the vocab **code-side** in `make_ref_accept` (per-field drop, as Task 1 already does) + enumerate it in the **prompt**. Net: Slice 2 bumps the **prompt to v3** only; the **schema stays v2**. (Optional future: separate strict wire-steering vs tolerant canonical schemas — out of scope.) Update Task 1 Step 4 (drop "Add the v3 synth schemas") and pass `prompt_version=3, schema_version=2` from the synth drivers.
- **[F2] Task 2 — read `supersedes_event_id` from the durable log, not the graph link.** `build_graph` DELETEs+recreates `graph_node` (link columns default NULL) at `ingest.py:1447`, **before** Pass B's `_propose_table_facts` (`:1555`). So reading `graph_node.<field>_decision_id` yields NULL. In `stale_and_clear_field`, set `supersedes_event_id` from `read_field_decisions(conn, logical_ref, field_name)` — the latest non-retired decision's `.decision_event_id` (None if empty). DROP the `_current_decision_id`/`graph_node`-SELECT. Mirrors `is_feature_eligible` (reads the decision log, never the flat column). `field_decision_event` survives `build_graph`.
- **[F14] Task 2/3/4 test fixes.** (c) In Task 3 Step 1 the scripted synthesis must **not** add `grain_columns_valid` (the synth object is `additionalProperties:false`) — script only `{"grain_columns": ["ghost"], "table_role": "wat"}`. (d) The stored decision enum is **lowercase** `"staled"` (`field_decision.py:43`) — assert `event_type == "staled"` (or `FieldDecisionEventType.STALED.value`), never `"STALED"`, in Tasks 2 & 4.
- **[F9] Task 2 — `prior_value_staled` from the staled COUNT, both directions.** `_write_producer_field` returns the staled-count `int` (`ingest.py:707`). (1) Set `prior_value_staled=True` whenever the dropped-absent stale `n > 0` — **decouple it from the clear-gate** (a human confirmation can remain active while LLM evidence is staled), keeping only `stale_and_clear_field` inside the `not _active_field_names(...)` gate. (2) In the **present**-value advisory write (`table_synth.py:422-428`), capture `staled = _write_producer_field(...)` and `_mark_staled(...)` when `staled > 0`, so a present-replaces-older change flags the accepted disposition too.
- **[F13] Task 1 — complete availability + event normalization.** In the availability block, fold `as_of_column` (`aoc.strip().lower()`) and match via `back = {c.lower(): c for c in cols}`, emitting the **canonical** table spelling; match `as_of_basis` as `aob.strip().lower() in _VALID_BASIS`. In the event block, capture `reos = s.get("event_or_snapshot")`; if it is a non-empty string but `eos is None`, `_put(ref, "event_or_snapshot", "dropped_invalid", "event_or_snapshot_off_vocab")` (add that reason code) rather than `abstained`. Add tests: case-variant `as_of_column` accepted with canonical spelling; invalid non-empty `event_or_snapshot` → `dropped_invalid`.
- **[F12] Task 3 — `not_evaluated` is FIVE field records per unresolved table.** `run_batched` returns only resolved refs (`enrich_batch.py:239`), so a table that never reached `make_ref_accept` has no disposition. For every assembled table absent from `syntheses`, emit **five** `not_evaluated` records (one per field: grain, availability_time, table_role, primary_entity, event_or_snapshot) so the record shape stays uniform/total. (If a coarse failure reason is wanted, add a parallel `table_outcomes` list — optional.)
