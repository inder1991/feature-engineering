# P2 — Independent AI Confidence Overlay: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute an *independent* AI-confidence for every AI-filled `concept`, store it in the existing P0 observation store, calibrate it into a rare "verification needed" flag, and surface it read-time in asset-detail and the feature considered-set — all non-blocking, behind a default-off flag.

**Architecture:** A new async pass reuses the P0 signal functions (`ground_concept` + `reclassify_concept` + `fuse`) to score AI-filled concepts and writes observations into `attestation_shadow_observation`, tagging its run `purpose='confidence'` (vs the existing `'measure'` runs). A calibration function in `report.py` turns the confidence float into band cut points against gold. Asset-detail (S1) and the considered-set route (S2) read the latest confidence observation and render an advisory — but only when the field is still AI-authored and the scored concept still matches the displayed one. Nothing reads the confidence in any eligibility/candidate/readiness path.

**Tech Stack:** Python 3.12, psycopg (raw SQL), pytest with an ephemeral-Postgres `overlay_conn` fixture, FastAPI routes, React/TypeScript frontend.

## Global Constraints

- **Flag:** `OVERLAY_AI_CONFIDENCE` (default `"0"`), read via `os.environ.get("OVERLAY_AI_CONFIDENCE", "0") == "1"` — the house idiom (matches `OVERLAY_PASS_C`/`OVERLAY_TABLE_SYNTH` in `ingest.py:105,113`). NOT via `get_settings()`.
- **Flag-off is byte-identical to today:** flag off ⇒ the confidence pass writes nothing AND S1/S2 read nothing AND emit no new response keys.
- **Measure-only preserved:** the confidence pass writes ONLY `attestation_shadow_run` + `attestation_shadow_observation`. It writes ZERO `field_evidence` / `field_decision_event` / `graph_node` rows. `reclassify_concept`'s own audited `llm_call`/`document_schema` telemetry is expected and is NOT an authority row.
- **No new confidence table:** reuse `attestation_shadow_observation` (migration 1018). The ONLY schema change is an additive `purpose` column on `attestation_shadow_run` (migration 1020).
- **Two render guards (both required):** surface a concept's confidence ONLY when (a) the field's active evidence author is `llm` AND (b) the observation's `proposer_value` equals the currently-displayed concept. Either guard failing ⇒ show no band (a human correction or an AI re-proposal auto-suppresses the stale band).
- **Non-blocking:** confidence is read by exactly two consumers — S1 (asset-detail) and S2 (considered-set). Nothing reads it in any eligibility, candidate-filter, readiness, or acceptance path. A `verification_needed` column MUST produce the identical candidate set + disposer outcome it does today.
- **Scope = `concept`** only (the field the P0 signals cover). `definition`/`domain` are out of scope.
- **Bands are provisional until real gold labels exist.** The confidence is a *signal*, never a gate.
- **Existing tests must stay green.** `field_evidence.confidence_band` and `readiness.py` are NOT touched.
- **Migration WORM:** 1018 tables have `BEFORE UPDATE OR DELETE` triggers — INSERT/SELECT only; never UPDATE/DELETE an `attestation_*` row.

## File Structure

- Create: `src/featuregen/db/migrations/1020_shadow_run_purpose.sql` — additive `purpose` column.
- Modify: `src/featuregen/overlay/upload/attest/shadow_store.py` — `ShadowRunV1.purpose` + `write_shadow_run` INSERT.
- Create: `src/featuregen/overlay/upload/attest/confidence.py` — the confidence pass + the flag + the latest-observation read helper + band mapping.
- Modify: `src/featuregen/overlay/upload/attest/report.py` — `calibrate_bands(...)` calibration output.
- Modify: `src/featuregen/overlay/upload/asset_detail.py` — S1 surfacing in `_effective_metadata_section`.
- Modify: `src/featuregen/api/routes/contract.py` — S2 `input_advisories` in `_considered_set_response`.
- Create: `tests/featuregen/overlay/upload/attest/test_confidence.py`, `tests/featuregen/overlay/upload/attest/test_calibration.py`, `tests/featuregen/overlay/upload/test_asset_detail_ai_confidence.py`, `tests/featuregen/api/routes/test_considered_set_advisory.py`, `tests/featuregen/overlay/upload/attest/test_confidence_correction.py`.
- Modify (Task 8, frontend): `frontend/src/screens/AssetDetailScreen.tsx` + considered-set screen — render the advisory (verified in CI; frontend vitest hangs locally per repo note).

---

### Task 1: Migration 1020 + `ShadowRunV1.purpose`

**Files:**
- Create: `src/featuregen/db/migrations/1020_shadow_run_purpose.sql`
- Modify: `src/featuregen/overlay/upload/attest/shadow_store.py:52-66` (dataclass), `:120-138` (`write_shadow_run`)
- Test: `tests/featuregen/overlay/upload/attest/test_confidence.py::test_shadow_run_purpose_roundtrip`

**Interfaces:**
- Produces: `ShadowRunV1(..., purpose: str = "measure")`; `write_shadow_run(conn, rec)` persists `purpose`.
- Consumes: existing `attestation_shadow_run` DDL (1018:68-94).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/attest/test_confidence.py
from datetime import UTC, datetime
from featuregen.overlay.upload.attest import shadow_store as S

def _run(**kw):
    base = dict(shadow_run_id="srun_p", catalog_source="src", gold_version_hash="gv",
                model_ids={"proposer": "m"}, signal_versions={"grounding": "1"},
                started_at=datetime(2026, 7, 22, tzinfo=UTC), sampled_keys=[("lr1", "concept")])
    base.update(kw)
    return S.ShadowRunV1(**base)

def test_shadow_run_purpose_roundtrip(overlay_conn):
    S.write_shadow_run(overlay_conn, _run(purpose="confidence"))
    row = overlay_conn.execute(
        "SELECT purpose FROM attestation_shadow_run WHERE shadow_run_id = 'srun_p'"
    ).fetchone()
    assert row[0] == "confidence"

def test_shadow_run_purpose_defaults_measure(overlay_conn):
    S.write_shadow_run(overlay_conn, _run(shadow_run_id="srun_m"))  # no purpose kw
    row = overlay_conn.execute(
        "SELECT purpose FROM attestation_shadow_run WHERE shadow_run_id = 'srun_m'"
    ).fetchone()
    assert row[0] == "measure"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/attest/test_confidence.py::test_shadow_run_purpose_roundtrip -x -q`
Expected: FAIL — `ShadowRunV1.__init__` has no `purpose`, and/or column missing.

- [ ] **Step 3: Write the migration**

```sql
-- src/featuregen/db/migrations/1020_shadow_run_purpose.sql
-- P2 AI-confidence overlay: distinguish full-coverage "confidence" runs from gold-sampled
-- "measure" runs so report.py's false-attest math filters to measurement runs only.
-- Additive + defaulted so existing rows and writers are unaffected.
ALTER TABLE attestation_shadow_run
    ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'measure'
        CHECK (purpose IN ('measure', 'confidence'));
```

- [ ] **Step 4: Add `purpose` to the dataclass + writer**

In `shadow_store.py`, add to `ShadowRunV1` (after `sampled_keys`, keep `column_count` property last):
```python
    purpose: str = "measure"
```
In `write_shadow_run`, add `purpose` to the INSERT column list + params (it currently INSERTs the other columns with `ON CONFLICT (shadow_run_id) DO NOTHING`):
```python
    # ... existing INSERT, add "purpose" to columns and rec.purpose to the values tuple ...
    #   INSERT INTO attestation_shadow_run
    #     (shadow_run_id, catalog_source, gold_version_hash, model_ids, signal_versions,
    #      started_at, sampled_keys, sampled_keys_hash, column_count, payload_hash, purpose)
    #   VALUES (%s, ..., %s) ON CONFLICT (shadow_run_id) DO NOTHING
```
Ensure `payload_hash` still hashes the run payload; include `purpose` in the hashed payload dict so the hash covers it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/attest/test_confidence.py -x -q`
Expected: PASS (both purpose tests). Then `uv run pytest tests/featuregen/overlay/upload/attest/test_runner.py -q` — existing runner tests still green (default `'measure'`).

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/db/migrations/1020_shadow_run_purpose.sql src/featuregen/overlay/upload/attest/shadow_store.py tests/featuregen/overlay/upload/attest/test_confidence.py
git commit -m "feat(p2): add purpose to attestation_shadow_run (migration 1020)"
```

---

### Task 2: The confidence pass (`attest/confidence.py`)

**Files:**
- Create: `src/featuregen/overlay/upload/attest/confidence.py`
- Test: `tests/featuregen/overlay/upload/attest/test_confidence.py` (extend)

**Interfaces:**
- Consumes: `ground_concept` (`grounding.py:179`), `reclassify_concept` (`reclassify.py:103`), `fuse` (`fusion.py:61`), `ColumnContext` (`reclassify.py:63`), `write_shadow_run`/`write_observation`/`ShadowRunV1`/`ObservationV1` (`shadow_store.py`), `parse_ref`, `DEFAULT_LLM_MODEL`, `record`-side enums from `evidence.py`.
- Produces:
  - `ai_confidence_enabled() -> bool`
  - `latest_confidence_observation(conn, logical_ref, field_name="concept") -> ConfObs | None` — newest `purpose='confidence'` observation for the ref (fields: `proposer_value`, `confidence`, `risk_tier`).
  - `run_confidence_pass(conn, catalog_source, *, client, run_id, gold_version_hash="unversioned", now=None) -> int` — scores AI-filled concepts (incremental) into a `purpose='confidence'` run; returns count scored.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/featuregen/overlay/upload/attest/test_confidence.py
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.attest.reclassify import _TASK as RECLASSIFY_TASK
from featuregen.overlay.upload.attest import confidence as C
from featuregen.overlay.field_evidence import record_field_evidence

def _seed_llm_concept(conn, logical_ref, value):
    record_field_evidence(conn, logical_ref=logical_ref, field_name="concept",
                          proposed_value=value, producer="llm", strength="proposed",
                          producer_ref="test", source_snapshot_id="snap", input_hash="h1")

def _authority_counts(conn):
    fe = conn.execute("SELECT count(*) FROM field_evidence").fetchone()[0]
    fde = conn.execute("SELECT count(*) FROM field_decision_event").fetchone()[0]
    gn = conn.execute("SELECT count(*) FROM graph_node").fetchone()[0]
    return fe, fde, gn

def test_confidence_pass_writes_observation_not_authority(overlay_conn, seeded_graph):
    # seeded_graph provides a catalog 'src' with a column whose logical_ref is known;
    # use its concept ref. (If seeded_graph is unavailable, build_graph a 1-col catalog.)
    lr = seeded_graph.concept_ref            # (catalog_source, schema, table, column) normalized
    _seed_llm_concept(overlay_conn, lr, "monetary_flow")
    client = FakeLLM(script={RECLASSIFY_TASK: FakeResponse(output={"concept": "monetary_flow"})})
    before = _authority_counts(overlay_conn)
    n = C.run_confidence_pass(overlay_conn, "src", client=client, run_id="crun_1")
    assert n == 1
    assert _authority_counts(overlay_conn) == before          # measure-only preserved
    obs = C.latest_confidence_observation(overlay_conn, lr)
    assert obs is not None and 0.0 <= obs.confidence <= 1.0
    assert obs.proposer_value == "monetary_flow"
    purpose = overlay_conn.execute(
        "SELECT purpose FROM attestation_shadow_run WHERE shadow_run_id = 'crun_1'").fetchone()[0]
    assert purpose == "confidence"

def test_confidence_pass_is_incremental_on_unchanged_value(overlay_conn, seeded_graph):
    lr = seeded_graph.concept_ref
    _seed_llm_concept(overlay_conn, lr, "monetary_flow")
    client = FakeLLM(script={RECLASSIFY_TASK: FakeResponse(output={"concept": "monetary_flow"})})
    C.run_confidence_pass(overlay_conn, "src", client=client, run_id="crun_a")
    n2 = C.run_confidence_pass(overlay_conn, "src", client=client, run_id="crun_b")
    assert n2 == 0                                             # unchanged concept -> skipped
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/featuregen/overlay/upload/attest/test_confidence.py -k confidence_pass -x -q`
Expected: FAIL — module `attest.confidence` does not exist.

- [ ] **Step 3: Implement `attest/confidence.py`**

```python
# src/featuregen/overlay/upload/attest/confidence.py
"""P2 — independent AI-confidence overlay (non-blocking, read-time).

Reuses the P0 signal functions to score every AI-filled `concept`, writing observations
into `attestation_shadow_observation` under a `purpose='confidence'` run. Writes NO
authority-tier rows (field_evidence/field_decision_event/graph_node) — measure-only, like
the P0 runner. Confidence is a signal, never a gate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.upload.attest.fusion import fuse
from featuregen.overlay.upload.attest.grounding import ground_concept
from featuregen.overlay.upload.attest.reclassify import ColumnContext, reclassify_concept
from featuregen.overlay.upload.attest.shadow_store import (
    ObservationV1, ShadowRunV1, write_observation, write_shadow_run)
from featuregen.overlay.upload.attest.runner import _latest_active_evidence, _risk_tier, _str_value
from featuregen.overlay.upload.model import parse_ref  # parse_ref(logical_ref) -> (source, schema, table, column)

_CONCEPT_FIELD = "concept"
_SIGNAL_VERSIONS = {"grounding": "1.0.0", "reclassify": "1.0.0", "fusion": "1.0.0"}


def ai_confidence_enabled() -> bool:
    return os.environ.get("OVERLAY_AI_CONFIDENCE", "0") == "1"


@dataclass(frozen=True, slots=True)
class ConfObs:
    proposer_value: str | None
    confidence: float
    risk_tier: str


def latest_confidence_observation(conn, logical_ref: str, field_name: str = _CONCEPT_FIELD) -> ConfObs | None:
    row = conn.execute(
        "SELECT o.proposer_value, o.confidence, o.risk_tier "
        "FROM attestation_shadow_observation o "
        "JOIN attestation_shadow_run r ON r.shadow_run_id = o.shadow_run_id "
        "WHERE o.logical_ref = %s AND o.field_name = %s AND r.purpose = 'confidence' "
        "ORDER BY r.started_at DESC, o.created_at DESC LIMIT 1",
        (logical_ref, field_name),
    ).fetchone()
    if row is None:
        return None
    return ConfObs(proposer_value=row[0], confidence=float(row[1]), risk_tier=row[2])


def _ai_filled_concept_keys(conn, catalog_source: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT logical_ref, proposed_value FROM field_evidence "
        "WHERE field_name = %s AND lifecycle = 'active' "
        "AND producer = %s AND strength = %s",
        (_CONCEPT_FIELD, EvidenceProducer.LLM.value, AssertionStrength.PROPOSED.value),
    ).fetchall()
    out: list[tuple[str, str]] = []
    for logical_ref, proposed_value in rows:
        if parse_ref(logical_ref)[0] != catalog_source:
            continue
        out.append((logical_ref, _str_value_of(proposed_value)))
    return out


def _str_value_of(proposed_value) -> str:
    # field_evidence.proposed_value is jsonb; concept is stored as a bare string value.
    return proposed_value if isinstance(proposed_value, str) else str(proposed_value)


def run_confidence_pass(conn, catalog_source: str, *, client, run_id: str,
                        gold_version_hash: str = "unversioned", now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    keys = _ai_filled_concept_keys(conn, catalog_source)
    to_score: list[tuple[str, str]] = []
    for logical_ref, concept_value in keys:
        prev = latest_confidence_observation(conn, logical_ref)
        if prev is not None and prev.proposer_value == concept_value:
            continue                              # incremental: unchanged concept -> skip
        to_score.append((logical_ref, concept_value))
    if not to_score:
        return 0
    write_shadow_run(conn, ShadowRunV1(
        shadow_run_id=run_id, catalog_source=catalog_source, gold_version_hash=gold_version_hash,
        model_ids={"proposer": _proposer_model(), "reclassifier": _proposer_model()},
        signal_versions=dict(_SIGNAL_VERSIONS), started_at=now,
        sampled_keys=[(lr, _CONCEPT_FIELD) for lr, _ in to_score], purpose="confidence"))
    for logical_ref, concept_value in to_score:
        proposer_ev = _latest_active_evidence(conn, logical_ref, _CONCEPT_FIELD,
                                              producer=EvidenceProducer.LLM.value)
        grounding = ground_concept(conn, logical_ref, concept_value or "")
        _s, _sc, _t, column = parse_ref(logical_ref)
        definition = _str_value(_latest_active_evidence(conn, logical_ref, "definition"))
        ctx = ColumnContext(name=column or "", definition=definition)
        reclassify_value = reclassify_concept(conn, client, logical_ref, column_ctx=ctx).value
        fusion = fuse(proposer_value=concept_value, reclassify_value=reclassify_value, grounding=grounding)
        agrees = None if reclassify_value is None else bool(fusion.agreement["proposer_reclassify_agree"])
        write_observation(conn, ObservationV1(
            shadow_run_id=run_id, logical_ref=logical_ref, field_name=_CONCEPT_FIELD,
            proposer_value=concept_value,
            proposer_producer=(proposer_ev.producer if proposer_ev else EvidenceProducer.LLM.value),
            reclassify_value=reclassify_value, reclassify_agrees=agrees,
            grounding_checks=dict(grounding.checks), grounding_coverage=grounding.coverage,
            grounding_conflict=grounding.conflict, confidence=fusion.confidence,
            risk_tier=_risk_tier(conn, logical_ref), created_at=now))
    return len(to_score)


def _proposer_model() -> str:
    from featuregen.overlay.upload.attest.runner import DEFAULT_LLM_MODEL  # reuse the single source
    return DEFAULT_LLM_MODEL
```

Note: if `_latest_active_evidence`, `_risk_tier`, `_str_value`, `DEFAULT_LLM_MODEL` are module-private in `runner.py`, either import them as shown (they exist per the interface extraction) or, if that couples too tightly, lift the tiny per-column scoring sequence into a shared `attest/_score.py` helper imported by both `runner.py` and `confidence.py` (DRY) — pick the lighter change during implementation and note it in the report.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/attest/test_confidence.py -x -q`
Expected: PASS. If `seeded_graph` lacks `.concept_ref`, build a 1-column catalog with `build_graph` + `normalize_ref` in the test helper instead (mirror `test_runner.py` seeding).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/attest/confidence.py tests/featuregen/overlay/upload/attest/test_confidence.py
git commit -m "feat(p2): confidence pass over AI-filled concepts (measure-only, incremental)"
```

---

### Task 3: Band calibration in `report.py`

**Files:**
- Modify: `src/featuregen/overlay/upload/attest/report.py`
- Test: `tests/featuregen/overlay/upload/attest/test_calibration.py`

**Interfaces:**
- Consumes: `_joined_rows(conn, shadow_run_id)` (`report.py:136-152`, yields `logical_ref, field_name, proposer_value, confidence, risk_tier, grounding_coverage, gold_value`), `_values_match` (`report.py:128`), `parse_ref`.
- Produces:
  - `Cuts = namedtuple("Cuts", "high_cut low_cut provisional")`
  - `band_for(confidence: float, cuts: Cuts) -> str` → `"high"|"medium"|"low"`
  - `verification_needed(confidence: float, cuts: Cuts) -> bool` (== band is `"low"`)
  - `calibrate_bands(conn, shadow_run_id, *, target_false_attest=0.05, min_gold=30) -> dict[tuple[str, str], Cuts]` keyed by `(catalog_source, field_name)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/attest/test_calibration.py
from featuregen.overlay.upload.attest import report as R
from featuregen.overlay.upload.attest.report import Cuts

def test_band_for_uses_cut_points():
    cuts = Cuts(high_cut=0.7, low_cut=0.35, provisional=True)
    assert R.band_for(0.80, cuts) == "high"
    assert R.band_for(0.50, cuts) == "medium"
    assert R.band_for(0.20, cuts) == "low"
    assert R.verification_needed(0.20, cuts) is True
    assert R.verification_needed(0.50, cuts) is False

def test_calibrate_bands_provisional_when_gold_thin(overlay_conn, tiny_confidence_run):
    # tiny_confidence_run: a shadow run + a handful of observations + <30 gold labels
    cuts = R.calibrate_bands(overlay_conn, tiny_confidence_run.run_id)
    key = ("src", "concept")
    assert cuts[key].provisional is True
    assert cuts[key].high_cut == 0.70 and cuts[key].low_cut == 0.35   # provisional defaults
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/featuregen/overlay/upload/attest/test_calibration.py -x -q`
Expected: FAIL — `Cuts`/`band_for`/`calibrate_bands` do not exist.

- [ ] **Step 3: Implement in `report.py`**

```python
# add near the top of report.py, after existing constants
from collections import namedtuple
from featuregen.overlay.upload.model import parse_ref

Cuts = namedtuple("Cuts", "high_cut low_cut provisional")
_HIGH_CUT_DEFAULT = 0.70
_LOW_CUT_DEFAULT = 0.35


def band_for(confidence: float, cuts: "Cuts") -> str:
    if confidence >= cuts.high_cut:
        return "high"
    if confidence < cuts.low_cut:
        return "low"
    return "medium"


def verification_needed(confidence: float, cuts: "Cuts") -> bool:
    return confidence < cuts.low_cut


def calibrate_bands(conn, shadow_run_id: str, *, target_false_attest: float = 0.05,
                    min_gold: int = 30) -> dict[tuple[str, str], "Cuts"]:
    """Choose per (catalog_source, field) band cut points from gold-joined observations.

    high_cut = the lowest confidence at which the gold false-attest rate among low-risk
    auto-attested rows stays <= target. low_cut (the verification-needed threshold) stays at
    the provisional default until there are enough gold labels to calibrate it — the flag is a
    signal, not a gate. provisional=True whenever gold n < min_gold.
    """
    groups: dict[tuple[str, str], list] = {}
    for r in _joined_rows(conn, shadow_run_id):
        source = parse_ref(r.logical_ref)[0]
        groups.setdefault((source, r.field_name), []).append(r)
    out: dict[tuple[str, str], Cuts] = {}
    for key, rows in groups.items():
        if len(rows) < min_gold:
            out[key] = Cuts(_HIGH_CUT_DEFAULT, _LOW_CUT_DEFAULT, True)
            continue
        high = _lowest_confidence_meeting_target(rows, target_false_attest)
        out[key] = Cuts(high, _LOW_CUT_DEFAULT, False)
    return out


def _lowest_confidence_meeting_target(rows, target: float) -> float:
    for c in THRESHOLDS:                       # 0.50 .. 0.95 ascending
        auto = [r for r in rows if r.confidence >= c and r.risk_tier == _LOW_RISK]
        if not auto:
            continue
        false_n = sum(1 for r in auto if not _values_match(r.proposer_value, r.gold_value))
        if false_n / len(auto) <= target:
            return c
    return 0.95
```

If `_JoinedRow` (report.py:118-126) does not already expose `logical_ref`, add it to `_joined_rows`' SELECT + the namedtuple (it selects `o.logical_ref` per the interface extraction, so it is available — confirm and use it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/attest/test_calibration.py -x -q`
Expected: PASS. Also `uv run pytest tests/featuregen/overlay/upload/attest/test_report.py -q` — existing report tests untouched (new functions are additive).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/attest/report.py tests/featuregen/overlay/upload/attest/test_calibration.py
git commit -m "feat(p2): band calibration (provisional-until-gold) over gold-joined observations"
```

---

### Task 4: S1 — asset-detail surfacing (with the two guards)

**Files:**
- Modify: `src/featuregen/overlay/upload/asset_detail.py:157-198` (`_effective_metadata_section`)
- Test: `tests/featuregen/overlay/upload/test_asset_detail_ai_confidence.py`

**Interfaces:**
- Consumes: `latest_confidence_observation` + `ai_confidence_enabled` (Task 2), `band_for`/`verification_needed`/`calibrate_bands` or provisional defaults (Task 3). The `active_ev` dict already carries `(producer, strength)` per field; `display_value = anchor.get("concept")` is the current concept.
- Produces: each concept field entry MAY carry `entry["ai_confidence"] = {"band": ..., "verification_needed": bool, "provisional": bool}` — else absent.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_asset_detail_ai_confidence.py
import os
from featuregen.overlay.upload.asset_detail import _effective_metadata_section
# ... seed a column with graph_node.concept='monetary_flow', an active llm/proposed concept
#     evidence, and a purpose='confidence' observation whose proposer_value='monetary_flow'
#     with a low confidence (e.g. 0.15).

def test_ai_confidence_shown_when_author_llm_and_value_matches(overlay_conn, monkeypatch, low_conf_column):
    monkeypatch.setenv("OVERLAY_AI_CONFIDENCE", "1")
    body = _effective_metadata_section(overlay_conn, low_conf_column.logical_ref, low_conf_column.anchor)
    ac = body["fields"]["concept"]["ai_confidence"]
    assert ac["verification_needed"] is True and ac["provisional"] is True

def test_ai_confidence_absent_when_flag_off(overlay_conn, monkeypatch, low_conf_column):
    monkeypatch.setenv("OVERLAY_AI_CONFIDENCE", "0")
    body = _effective_metadata_section(overlay_conn, low_conf_column.logical_ref, low_conf_column.anchor)
    assert "ai_confidence" not in body["fields"]["concept"]

def test_ai_confidence_suppressed_when_concept_value_changed(overlay_conn, monkeypatch, low_conf_column):
    monkeypatch.setenv("OVERLAY_AI_CONFIDENCE", "1")
    anchor = {**low_conf_column.anchor, "concept": "something_else"}   # displayed != scored
    body = _effective_metadata_section(overlay_conn, low_conf_column.logical_ref, anchor)
    assert "ai_confidence" not in body["fields"]["concept"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/featuregen/overlay/upload/test_asset_detail_ai_confidence.py -x -q`
Expected: FAIL — no `ai_confidence` key produced.

- [ ] **Step 3: Implement in `_effective_metadata_section`**

Inside the `for label, flat_col, c1_field in _METADATA_FIELDS:` loop, after `entry["evidence_provenance"] = ...`, add:
```python
        if c1_field == "concept" and ai_confidence_enabled():
            ev = active_ev.get("concept")
            if ev == ("llm", "proposed"):                    # guard 1: still AI-authored
                obs = latest_confidence_observation(conn, logical_ref)
                if obs is not None and obs.proposer_value == display_value:  # guard 2: value match
                    cuts = _concept_cuts(conn, logical_ref)  # provisional defaults for now
                    entry["ai_confidence"] = {
                        "band": band_for(obs.confidence, cuts),
                        "verification_needed": verification_needed(obs.confidence, cuts),
                        "provisional": cuts.provisional,
                    }
```
Add imports at the top of `asset_detail.py`:
```python
from featuregen.overlay.upload.attest.confidence import ai_confidence_enabled, latest_confidence_observation
from featuregen.overlay.upload.attest.report import Cuts, band_for, verification_needed
```
And a small helper (provisional cuts until a persisted calibration artifact exists — a follow-on):
```python
def _concept_cuts(conn, logical_ref: str) -> Cuts:
    # Provisional defaults; a persisted per-source calibration artifact (from report.calibrate_bands)
    # replaces this once real gold labels exist. Signal, not a gate.
    return Cuts(0.70, 0.35, True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/test_asset_detail_ai_confidence.py -x -q`
Expected: PASS. Then `uv run pytest tests/featuregen/overlay/upload/ -k asset_detail -q` — existing asset-detail tests green (new key only appears flag-on + guards-pass).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/asset_detail.py tests/featuregen/overlay/upload/test_asset_detail_ai_confidence.py
git commit -m "feat(p2): S1 asset-detail AI-confidence surfacing behind two guards"
```

---

### Task 5: S2 — considered-set advisory (`input_advisories`)

**Files:**
- Modify: `src/featuregen/api/routes/contract.py:214-220` (`_considered_set_response`)
- Test: `tests/featuregen/api/routes/test_considered_set_advisory.py`

**Interfaces:**
- Consumes: `logical_ref_of` (`column_authority.py:56`), `latest_confidence_observation` + `ai_confidence_enabled` (Task 2), `band_for`/`verification_needed` (Task 3). Each `FeatureIdea.derives_pairs` is `tuple[(catalog_source, object_ref)]`.
- Produces: the considered-set response gains a top-level `"input_advisories": list[{"catalog_source", "object_ref", "message"}]` — one per distinct low-confidence input column across anchor + alternatives. Empty list (or omitted) when flag off.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/api/routes/test_considered_set_advisory.py
# Drive POST /contract/considered-set (or _considered_set_response directly) with an intent whose
# candidate features draw from a column that has a purpose='confidence' obs, verification_needed,
# value-matching + llm-authored. Assert (a) input_advisories contains that column and (b) the
# feature is still present in the response (non-blocking).

def test_input_advisory_present_and_non_blocking(considered_set_with_low_conf_input, monkeypatch):
    monkeypatch.setenv("OVERLAY_AI_CONFIDENCE", "1")
    resp = considered_set_with_low_conf_input()          # calls _considered_set_response
    advisories = resp["input_advisories"]
    assert any(a["object_ref"] == "public.txn.amt" for a in advisories)
    # feature still offered — advisory did not remove it
    assert resp["anchor"] is not None or resp["alternatives"]

def test_no_advisories_when_flag_off(considered_set_with_low_conf_input, monkeypatch):
    monkeypatch.setenv("OVERLAY_AI_CONFIDENCE", "0")
    resp = considered_set_with_low_conf_input()
    assert resp.get("input_advisories", []) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/featuregen/api/routes/test_considered_set_advisory.py -x -q`
Expected: FAIL — no `input_advisories` key.

- [ ] **Step 3: Implement in `_considered_set_response`**

```python
def _considered_set_response(intent, cs, conn=None) -> dict:   # add conn param (thread from caller)
    resp = {"intent_id": intent.intent_id, "anchor": cs.anchor,
            "alternatives": cs.alternatives, "recommendation": cs.recommendation,
            "rejections": cs.rejections}
    resp["input_advisories"] = _input_advisories(conn, cs) if conn is not None else []
    return resp


def _input_advisories(conn, cs) -> list[dict]:
    from featuregen.overlay.upload.attest.confidence import (
        ai_confidence_enabled, latest_confidence_observation)
    from featuregen.overlay.upload.attest.report import Cuts, verification_needed
    from featuregen.overlay.upload.column_authority import logical_ref_of
    if not ai_confidence_enabled():
        return []
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    ideas = ([cs.anchor] if cs.anchor else []) + [f for fs in cs.alternatives for f in fs.features]
    for idea in ideas:
        for catalog_source, object_ref in getattr(idea, "derives_pairs", ()):
            k = (catalog_source, object_ref)
            if k in seen:
                continue
            seen.add(k)
            lr = logical_ref_of(conn, catalog_source, object_ref)
            obs = latest_confidence_observation(conn, lr)
            if obs is None:
                continue
            cuts = Cuts(0.70, 0.35, True)               # provisional; matches S1 _concept_cuts
            if verification_needed(obs.confidence, cuts):
                out.append({"catalog_source": catalog_source, "object_ref": object_ref,
                            "message": f"Input '{object_ref}': meaning is AI-filled · "
                                       "verification needed — its derived additivity/timing rests on that."})
    return out
```
Thread `conn` into the two call sites: `_considered_set_response(intent, cs)` at contract.py:214 usage inside `considered_set` (508-552) and `_scoped_considered_set` (462) — both handlers already hold `conn` (route dependency). Note: the guard "author is llm AND value matches" is enforced inside `latest_confidence_observation` only for the value part; add the same `("llm","proposed")` author check by reading the concept's active evidence for `lr` (reuse the S1 pattern) so a human-corrected input shows no advisory. Keep it non-blocking: `input_advisories` is additive; no feature is removed or altered.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/featuregen/api/routes/test_considered_set_advisory.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/api/routes/contract.py tests/featuregen/api/routes/test_considered_set_advisory.py
git commit -m "feat(p2): S2 considered-set input_advisories (additive, non-blocking)"
```

---

### Task 6: Flag-off byte-identity + the non-blocking guarantee test

**Files:**
- Test: `tests/featuregen/overlay/upload/attest/test_confidence.py` (extend) + `tests/featuregen/api/routes/test_considered_set_advisory.py` (extend)

**Interfaces:** Consumes everything above. No new production code — this task proves the Global Constraints hold.

- [ ] **Step 1: Write the tests**

```python
def test_verification_needed_column_yields_identical_candidate_set(considered_set_with_low_conf_input, monkeypatch):
    # Same intent/catalog, two runs: (A) no confidence observation at all,
    # (B) a verification_needed observation present + flag on.
    # Assert the set of offered feature names is identical (non-blocking).
    monkeypatch.setenv("OVERLAY_AI_CONFIDENCE", "1")
    names_with = _feature_names(considered_set_with_low_conf_input(observe=True))
    names_without = _feature_names(considered_set_with_low_conf_input(observe=False))
    assert names_with == names_without

def test_flag_off_body_has_no_confidence_keys(overlay_conn, monkeypatch, low_conf_column):
    monkeypatch.setenv("OVERLAY_AI_CONFIDENCE", "0")
    body = _effective_metadata_section(overlay_conn, low_conf_column.logical_ref, low_conf_column.anchor)
    assert "ai_confidence" not in body["fields"]["concept"]
```

- [ ] **Step 2: Run — expect PASS (behavior already built).** If either fails, the guard/gating is wrong; fix the gating, not the test.

Run: `uv run pytest tests/featuregen/overlay/upload/attest/test_confidence.py tests/featuregen/api/routes/test_considered_set_advisory.py -q`

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test(p2): flag-off byte-identity + non-blocking candidate-set guarantees"
```

---

### Task 7: S3 — correction-in-arrears suppresses the AI band (test only)

**Files:**
- Test: `tests/featuregen/overlay/upload/attest/test_confidence_correction.py`

**Interfaces:** Consumes `apply_field_correction` (`field_correction.py:226`, confirm-override path writes `human/confirmed` + `resolve_and_project`). No new production code — the S1/S2 author-guard already suppresses the band once the active author flips to `human`.

- [ ] **Step 1: Write the test**

```python
def test_human_correction_suppresses_ai_band(overlay_conn, human_actor, low_conf_column, monkeypatch):
    monkeypatch.setenv("OVERLAY_AI_CONFIDENCE", "1")
    # precondition: band shown
    before = _effective_metadata_section(overlay_conn, low_conf_column.logical_ref, low_conf_column.anchor)
    assert "ai_confidence" in before["fields"]["concept"]
    # human confirms an override on concept (two-eyes as required by the path)
    apply_field_correction(overlay_conn, source="src", object_ref=low_conf_column.object_ref,
                           field="concept", action="confirm_override", actor=human_actor,
                           idempotency_key="k1", expected_latest_decision_id=..., replacement_value="monetary_flow",
                           ...)  # supply the four-eyes / expected-* args per the route contract
    anchor2 = _reload_anchor(overlay_conn, low_conf_column.logical_ref)  # active author now human
    after = _effective_metadata_section(overlay_conn, low_conf_column.logical_ref, anchor2)
    assert "ai_confidence" not in after["fields"]["concept"]   # author-guard suppressed it
```

- [ ] **Step 2: Run to verify it passes** (behavior already built by Task 4's guard).

Run: `uv run pytest tests/featuregen/overlay/upload/attest/test_confidence_correction.py -x -q`
Expected: PASS. If the four-eyes/`expected_*` args make this heavy, assert the guard directly: after a `human/confirmed` concept row exists, `active_ev["concept"] != ("llm","proposed")` so S1 omits `ai_confidence`.

- [ ] **Step 3: Commit**

```bash
git add tests/featuregen/overlay/upload/attest/test_confidence_correction.py
git commit -m "test(p2): human correction suppresses the AI band via the author-guard"
```

---

### Task 8: Frontend rendering (thin, CI-verified)

**Files:**
- Modify: `frontend/src/screens/AssetDetailScreen.tsx` — render `entry.ai_confidence`: when `verification_needed`, a prominent "AI-filled · verification needed" chip; else "AI-filled · {band}". Show a "provisional" hint when `provisional`.
- Modify: the considered-set screen — render `input_advisories` as an advisory note per matching input column.

**Interfaces:** Consumes the backend `ai_confidence` object (Task 4) and `input_advisories` list (Task 5).

- [ ] **Step 1: Write the failing component test** (Vitest) asserting the chip renders for a `verification_needed` entry and does NOT render when the key is absent.
- [ ] **Step 2: Run** `cd frontend && npx vitest run src/screens/AssetDetailScreen.test.tsx` — expect FAIL. (Full `vitest` hangs on worker-start in this env per the repo note; run the single changed file, and rely on CI for the whole suite.)
- [ ] **Step 3: Implement** the chip + advisory rendering (match existing chip/label styling in the screen).
- [ ] **Step 4: Run** the single test file — expect PASS.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/screens/AssetDetailScreen.tsx frontend/src/screens/AssetDetailScreen.test.tsx
git commit -m "feat(p2): render AI-confidence chip + considered-set advisory (frontend)"
```

---

## Self-Review (author checklist — completed)

- **Spec coverage:** carrier (reuse 1018) → Task 1; confidence pass → Task 2; calibration → Task 3; S1 → Task 4; S2 → Task 5; flag + non-blocking → Task 6; S3 → Task 7; frontend → Task 8. All spec §S1/S2/S3 + Decomposition items 1-7 map to a task.
- **Measure-only:** Task 2 test asserts zero authority-table writes (mirrors `test_run_shadow_writes_no_authority_state`).
- **Two guards:** Task 4 (author=llm ∧ value-match) with explicit suppression tests; Task 5 repeats the author guard; Task 7 proves the correction path suppression.
- **Non-blocking:** Task 6 asserts identical candidate set with/without the observation.
- **Provisional:** Tasks 3/4/5 stamp `provisional=True` until real gold calibration.
- **Type consistency:** `Cuts`, `ConfObs`, `latest_confidence_observation`, `ai_confidence_enabled`, `band_for`, `verification_needed`, `calibrate_bands` are referenced with identical signatures across tasks.
- **No new confidence table; only additive migration 1020.** `field_evidence.confidence_band` / `readiness.py` untouched.

## Post-build gate (before OVERLAY_AI_CONFIDENCE goes live)

The machinery ships behind the default-off flag. Going live requires the two provisional pieces to become real: (1) real human gold labels (the P0 labelling protocol) so `calibrate_bands` produces a calibrated (non-provisional) low threshold, and (2) a persisted calibration artifact that S1's `_concept_cuts` and S2 read instead of the provisional defaults. Until then, bands render `provisional` and the flag stays off in production.
