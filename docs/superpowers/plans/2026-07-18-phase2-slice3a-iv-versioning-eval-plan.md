# Slice 3A-iv — Versioning + Flag Byte-Identity + Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (- [ ]) syntax.

**Goal:** Register v2 feature-gen output schemas, thread real numeric prompt/schema versions through every feature-assist call, add explicit v1/v2 `FeatureIdea` serializers gated by a single flag so a flag-OFF response and considered-set snapshot stay byte-identical to pre-Slice-3, and ship the concrete real-provider quality gate (curated gold set + hermetic metric units + key-gated baseline-vs-enriched eval).

**Architecture:** The feature-gen enrichment is versioned but permissive — v2 output schemas are byte-for-byte v1 aliases (semantic validation stays code-side in `_validate_idea`); the version numbers merely stamp which INPUT contract egressed. A single env flag `FEATUREGEN_FEATURE_CONTEXT` (default off), read once at the boundary via `feature_context_enabled()`, gates the versioned shape: when off, explicit v1 serializers strip the new `FeatureIdea` fields at the assist routes and the Gate #1 snapshot, guaranteeing byte-identity. The quality gate is a hermetic metric module + a ≥40-case versioned gold artifact (both CI gates) plus a manually-run, key-gated eval that measures the baseline (thin menu) vs enriched (flag on) run against the pinned real provider and writes a versioned report.

## Global Constraints

- **Branch base:** the **3A-iii branch tip** (which is based on 3A-ii → 3A-i). All shared types from plan 3A-i (`Requirement`, `REQUIREMENT_CODES`, `VALIDATION_STATES`, the extended `FeatureIdea`, tri-state `_validate_idea`), plan 3A-ii (`ContractDraft.validation_status/requirements`, extended `_idea_json`/`_idea_from_json`, the `contract` columns), and plan 3A-iii (menu widening, nested egress adapter, deterministic relevance) are **already present** on the base. Do not re-create them.
- **Implementers on FABLE; reviews on OPUS.** Set the model explicitly per subagent.
- **Shared-interface names (verbatim — do not redefine or drift):**
  - `FeatureIdea` fields added by 3A-i: `operation_kind: str = ""`, `measure_refs: tuple[tuple[str,str],...] = ()`, `grain_ref: tuple[str,str] | None = None`, `time_ref: tuple[str,str] | None = None`, `window: str | None = None`, `grouping_refs: tuple[tuple[str,str],...] = ()`, `validation_status: str = "DESIGN_CHECKED"`, `requirements: tuple[Requirement,...] = ()`. The pre-existing `verification: str = "DESIGN-CHECKED"` hyphenated stamp STAYS as a SEPARATE axis.
  - `Requirement(code: str, operand: tuple[str,str], detail: str = "")` — `@dataclass(frozen=True, slots=True)`.
  - `VALIDATION_STATES = ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION", "REJECTED")`.
  - Flag: env `FEATUREGEN_FEATURE_CONTEXT` (default off), **captured once at the route**.
  - v2 output schemas: `("feature_ideas", 2)` (+ `feature_recipe`/`leakage`/`feature_set_rec` v2). Code-side semantic validation stays in `_validate_idea`.
  - `_call_raw` gains `prompt_version: int = 1, schema_version: int = 1`, threaded to `audited_structured_call` (which already accepts them), passed at all 7 call sites.
- **Run pytest DIRECTLY** with the repo interpreter: `.venv/bin/python -m pytest <path> -q`. **Never pipe through `| tail`.** The eval suite is excluded by default (`addopts = -m 'not eval'`); run it explicitly with `-m eval`.
- **ruff line-length 100.** Run `.venv/bin/python -m ruff check <changed files>` before each commit.
- **No placeholders / no `...` in tests** — concrete assertions everywhere.

---

## Task 1 — Register v2 feature-gen output schemas

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (the `_SCHEMAS` dict, near the synth v2-alias loop at ~line 423)
- Test: `tests/featuregen/overlay/upload/test_feature_v2_schemas.py` (create)

**Interfaces:**
- Consumes: the existing permissive v1 entries `("feature_ideas",1)`, `("feature_recipe",1)`, `("leakage",1)`, `("feature_set_rec",1)` in `_SCHEMAS`; `register_enrichment_schemas(conn)`; `DocumentSchemaRegistry.schema_for(type_name, schema_version) -> dict | None`.
- Produces: registered `("feature_ideas",2)`, `("feature_recipe",2)`, `("leakage",2)`, `("feature_set_rec",2)` as intentional v1 aliases (output body byte-for-byte v1; semantic validation stays code-side per spec §8). No new schema body — the permissive `additionalProperties: True` shape already admits the new proposal fields.

Steps:

- [ ] Write the failing test `tests/featuregen/overlay/upload/test_feature_v2_schemas.py`:
```python
"""Slice 3A-iv Task 1: the feature-gen v2 output schemas register and resolve as v1 aliases.

v1 stays permissive and semantic validation stays code-side in `_validate_idea` — v2 exists only to
stamp which INPUT contract egressed (spec §8), so the OUTPUT body must be byte-for-byte v1."""
from __future__ import annotations

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.overlay.upload.enrich_llm import _SCHEMAS, register_enrichment_schemas

_FEATURE_IDS = ("feature_ideas", "feature_recipe", "leakage", "feature_set_rec")


def test_feature_v2_present_in_schemas_dict_as_v1_alias():
    for sid in _FEATURE_IDS:
        assert (sid, 1) in _SCHEMAS, f"{sid} v1 missing"
        assert (sid, 2) in _SCHEMAS, f"{sid} v2 not registered in _SCHEMAS"
        # Intentional alias: the SAME object, so the two versions can never drift.
        assert _SCHEMAS[(sid, 2)] is _SCHEMAS[(sid, 1)], f"{sid} v2 must be the v1 object"


def test_feature_v2_resolves_through_registry(db):
    register_enrichment_schemas(db)
    reg = DocumentSchemaRegistry(db)
    for sid in _FEATURE_IDS:
        v1 = reg.schema_for(sid, 1)
        v2 = reg.schema_for(sid, 2)
        assert v1 is not None, f"{sid} v1 did not register"
        assert v2 is not None, f"{sid} v2 did not register"
        assert v2 == v1, f"{sid} v2 body must equal v1 body"


def test_bootstrap_provider_compat_guard_still_passes(db):
    # register_enrichment_schemas asserts every schema projects to an Anthropic-compatible wire
    # schema before touching the DB; adding the v2 aliases must not trip that guard.
    register_enrichment_schemas(db)
```

- [ ] Run it and expect FAIL (the `(sid, 2)` entries do not exist yet):
```
.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_v2_schemas.py -q
```

- [ ] Implement in `enrich_llm.py`. Locate the existing synth v2-alias loop (find by symbol: `for _synth_schema_id in (`, ~line 423). Immediately AFTER that loop, add the feature-schema alias loop:
```python
# Feature-assist v2 (Phase-2 Slice 3A-iv, spec §8): the OUTPUT contract is byte-for-byte v1 — the
# permissive `additionalProperties: True` shape already admits the new proposal fields, and semantic
# validation stays CODE-SIDE in `_validate_idea`. v2 exists only so the immutable `llm_call` record
# stamps WHICH input contract egressed (the widened menu / tri-state shape) instead of a hardcoded 1
# masked by a `…_v1` prompt_id string. Registered as real v2 rows via `register_enrichment_schemas`'s
# `_SCHEMAS` sweep so `schema_for(schema_id, 2)` resolves them; an intentional alias, never a copy.
for _feature_schema_id in ("feature_ideas", "feature_recipe", "leakage", "feature_set_rec"):
    _SCHEMAS[(_feature_schema_id, 2)] = _SCHEMAS[(_feature_schema_id, 1)]
```

- [ ] Run the test and expect PASS:
```
.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_v2_schemas.py -q
```

- [ ] Lint, then commit:
```
.venv/bin/python -m ruff check src/featuregen/overlay/upload/enrich_llm.py tests/featuregen/overlay/upload/test_feature_v2_schemas.py
git add -A && git commit -m "feat(3A-iv): register feature-gen v2 output schemas as v1 aliases"
```

---

## Task 2 — Flag helper + thread prompt_version/schema_version through `_call_raw` at all 7 sites

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (`_call_raw` signature + body; the 7 `_call_raw` call sites; add `feature_context_enabled()`)
- Test: `tests/featuregen/overlay/upload/test_feature_version_threading.py` (create)

**Interfaces:**
- Consumes: `audited_structured_call(conn, client, *, task, prompt_id, schema_id, catalog_metadata, instruction, actor=None, prompt_version=1, schema_version=1)` (already accepts the version kwargs, defaulting 1); the v2 schemas from Task 1; `os.environ`.
- Produces: `feature_context_enabled() -> bool` (reads `FEATUREGEN_FEATURE_CONTEXT`, default off, idiom `== "1"`); `_call_raw(..., *, actor=None, prompt_version: int = 1, schema_version: int = 1) -> dict` threading both to `audited_structured_call`. The 6 feature-schema call sites (recommend×2, refine, recipe, leakage, feature-set) pass `prompt_version`/`schema_version` = `2 if feature_context_enabled() else 1`; the critique call site stays version 1 (no `feature_candidate_critique` v2 exists — spec §8 registers only the 4 feature schemas). The immutable `llm_call` records the real numeric version instead of a hardcoded 1.

Steps:

- [ ] Write the failing test `tests/featuregen/overlay/upload/test_feature_version_threading.py`:
```python
"""Slice 3A-iv Task 2: the feature-gen versions thread to `audited_structured_call` and land on the
immutable `llm_call` record — 2 when FEATUREGEN_FEATURE_CONTEXT is on, 1 when off (byte-for-byte v1)."""
from __future__ import annotations

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import feature_context_enabled, recommend_features
from featuregen.overlay.upload.graph import build_graph


def _bank_graph(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("bank", "transactions", "amount", "numeric", definition="txn amount",
                     additivity="additive", unit="dollars", currency="USD", entity="Transaction"),
        CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True, entity="Account"),
        CanonicalRow("bank", "accounts", "churned", "boolean", definition="customer churned flag"),
    ])


def _fake():
    return FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "txn_count_90d", "description": "count of txns",
         "derives_from": ["public.transactions.amount"], "aggregation": "count", "grain_table": "accounts"},
    ]})})


def _feature_ideas_versions(db):
    return db.execute(
        "SELECT output_schema_version, prompt_version FROM llm_call "
        "WHERE output_schema_id = 'feature_ideas'").fetchall()


def test_flag_default_is_off():
    assert feature_context_enabled() is False


def test_versions_are_1_when_flag_off(db, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    _bank_graph(db)
    recommend_features(db, "predict churn", _fake(), catalog_source="bank", critic=False)
    rows = _feature_ideas_versions(db)
    assert rows, "recommend must record at least one feature_ideas llm_call"
    assert all(tuple(r) == (1, 1) for r in rows), rows


def test_versions_are_2_when_flag_on(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    assert feature_context_enabled() is True
    _bank_graph(db)
    recommend_features(db, "predict churn", _fake(), catalog_source="bank", critic=False)
    rows = _feature_ideas_versions(db)
    assert rows, "recommend must record at least one feature_ideas llm_call"
    assert all(tuple(r) == (2, 2) for r in rows), rows
```

- [ ] Run it and expect FAIL (`feature_context_enabled` does not exist; versions are hardcoded 1):
```
.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_version_threading.py -q
```

- [ ] Implement in `feature_assist.py`. First confirm `import os` is present at the top (add it to the stdlib import block if absent — it currently imports `logging`, `re`). Add the flag helper immediately after the module logger line (`logger = logging.getLogger(__name__)`):
```python
def feature_context_enabled() -> bool:
    """Single gate for the Slice-3 feature-context enrichment (menu widening, tri-state emission, the
    versioned v2 shape). Default OFF: the whole enrichment is inert and the flag-OFF response/snapshot
    stay byte-identical to pre-Slice-3 (spec §8). Read once at the route; env is immutable within a
    request, so internal reads here resolve to the same value the route captured."""
    return os.environ.get("FEATUREGEN_FEATURE_CONTEXT", "0") == "1"


def _feature_schema_version() -> int:
    """2 when the feature-context flag is on (the widened INPUT contract egressed), else 1 — so the
    immutable llm_call stamps the real numeric version, not a hardcoded 1 masked by a `…_v1` prompt_id."""
    return 2 if feature_context_enabled() else 1
```

- [ ] Extend `_call_raw` (find by symbol `def _call_raw(`). Change its signature to add the two version kwargs and thread them to `audited_structured_call`:
```python
def _call_raw(conn, client: LLMClient, task: str, prompt_id: str, schema_id: str,
              instruction: str, catalog_metadata: dict, *,
              actor: IdentityEnvelope | None = None,
              prompt_version: int = 1, schema_version: int = 1) -> dict:
    """Every feature-assist LLM call goes through the AUDITED seam (M6): the egress guard scans the
    user text (`instruction`) + metadata before dispatch, and the call is recorded in llm_call.
    `prompt_version`/`schema_version` (default 1 — byte-for-byte v1) pin the request's contract so the
    immutable record stamps WHICH input contract egressed, not a hardcoded 1. `actor` is the HUMAN
    subject the route threaded in; absent, the seam falls back to the service identity."""
    out = audited_structured_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, instruction=instruction, actor=actor,
        prompt_version=prompt_version, schema_version=schema_version)
    return out if isinstance(out, dict) else {}
```

- [ ] Thread the version at each of the **6 feature-schema** call sites. Locate each by symbol and add `prompt_version=_feature_schema_version(), schema_version=_feature_schema_version()` to the `_call_raw(...)` kwargs:
  1. `_fix_pass` — `_call_raw(conn, client, "overlay.feature.recommend", "feature_recommend_v1", "feature_ideas", objective, inputs, actor=actor, prompt_version=_feature_schema_version(), schema_version=_feature_schema_version())`
  2. `_generate` Phase 1 — same kwargs on the `"overlay.feature.recommend"` / `"feature_ideas"` call.
  3. `refine_idea` — same kwargs on the `"overlay.feature.recommend"` / `"feature_ideas"` call.
  4. `feature_recipe` — same kwargs on the `"overlay.feature.recipe"` / `"feature_recipe"` call.
  5. `leakage_check` — same kwargs on the `"overlay.feature.leakage"` / `"leakage"` call.
  6. `recommend_set` — same kwargs on the `"overlay.feature.recommend_set"` / `"feature_set_rec"` call.

- [ ] Leave the **7th** call site (`_critique_candidates`, `"overlay.feature.critique_candidates"` / `"feature_candidate_critique"`) at the default version 1 — spec §8 registers no `feature_candidate_critique` v2. Add a short inline comment there: `# critique stays v1 (no feature_candidate_critique v2 registered — spec §8)`.

- [ ] Run the test and expect PASS:
```
.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_version_threading.py -q
```

- [ ] Run the existing feature-assist suites to confirm no regression (FakeLLM resolves on `(task, prompt_id, input_hash)` / task-key, never on version — version changes must not break resolution):
```
.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_feature_assist.py tests/featuregen/overlay/upload/test_feature_loop.py tests/featuregen/api/test_feature_assist.py -q
```

- [ ] Lint, then commit:
```
.venv/bin/python -m ruff check src/featuregen/overlay/upload/feature_assist.py tests/featuregen/overlay/upload/test_feature_version_threading.py
git add -A && git commit -m "feat(3A-iv): thread prompt/schema versions through _call_raw at all 7 sites + flag helper"
```

---

## Task 3 — Explicit v1/v2 `FeatureIdea` serializers + flag capture at the assist routes (recommend byte-identity)

**Files:**
- Create: `src/featuregen/api/feature_serialize.py`
- Modify: `src/featuregen/api/routes/assist.py` (`recommend`, `refine`, `recommend_sets` route bodies)
- Test: `tests/featuregen/api/test_feature_serialize.py` (create)

**Interfaces:**
- Consumes: `FeatureIdea`, `Requirement`, `feature_context_enabled` from `featuregen.overlay.upload.feature_assist`.
- Produces:
  - `serialize_feature_idea_v1(idea: FeatureIdea) -> dict` — EXACTLY the pre-Slice-3 field set/order: `name, description, derives_from, aggregation, grain_table, derives_pairs, verification, critic_note, rationale` (`derives_pairs` as a list of `[catalog, ref]` lists). The new fields NEVER appear.
  - `serialize_feature_idea_v2(idea: FeatureIdea) -> dict` — the v1 dict plus `operation_kind, measure_refs, grain_ref, time_ref, window, grouping_refs, validation_status, requirements` (refs as lists / list-of-lists; each requirement `{"code","operand","detail"}`).
  - `serialize_feature_idea(idea: FeatureIdea, *, feature_context: bool) -> dict` — dispatch (v2 iff `feature_context`).
  - Routes capture `feature_context = feature_context_enabled()` ONCE at the top of the handler and serialize every `FeatureIdea` through it (proposals; refine's `revised`; recommend-sets' `sets[].features`).

Steps:

- [ ] Write the failing test `tests/featuregen/api/test_feature_serialize.py`:
```python
"""Slice 3A-iv Task 3: explicit v1/v2 FeatureIdea serializers.

Flag-OFF (v1) output must be BYTE-IDENTICAL to the pre-Slice-3 dataclass serialization even when the
new fields carry non-default values — the new fields must NOT leak (spec §8)."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from featuregen.api.feature_serialize import (
    serialize_feature_idea,
    serialize_feature_idea_v1,
    serialize_feature_idea_v2,
)
from featuregen.overlay.upload.feature_assist import FeatureIdea, Requirement
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv
from featuregen.intake.llm import FakeLLM, FakeResponse

# The exact key order FastAPI's jsonable_encoder produced for the pre-Slice-3 dataclass (field order:
# name, description, derives_from, aggregation, grain_table, derives_pairs, verification,
# critic_note, rationale). Byte-identity is asserted against this reference.
_PRE_SLICE3_KEYS = ["name", "description", "derives_from", "aggregation", "grain_table",
                    "derives_pairs", "verification", "critic_note", "rationale"]


def _idea_with_new_fields() -> FeatureIdea:
    # A fully-populated idea: new fields set to NON-default values so a leak would be visible.
    return FeatureIdea(
        name="avg_balance", description="average balance per account",
        derives_from=["public.accounts.balance"], aggregation="avg", grain_table="accounts",
        derives_pairs=(("deposits", "public.accounts.balance"),),
        verification="DESIGN-CHECKED", critic_note="note", rationale="why",
        operation_kind="avg", measure_refs=(("deposits", "public.accounts.balance"),),
        grain_ref=("deposits", "public.accounts.id"), time_ref=None, window="30d",
        grouping_refs=(("deposits", "public.accounts.cust_id"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("deposits", "public.accounts.balance"), "verify"),))


def test_v1_is_byte_identical_to_pre_slice3_reference():
    idea = _idea_with_new_fields()
    reference = {
        "name": "avg_balance", "description": "average balance per account",
        "derives_from": ["public.accounts.balance"], "aggregation": "avg", "grain_table": "accounts",
        "derives_pairs": [["deposits", "public.accounts.balance"]],
        "verification": "DESIGN-CHECKED", "critic_note": "note", "rationale": "why",
    }
    out = serialize_feature_idea_v1(idea)
    assert list(out.keys()) == _PRE_SLICE3_KEYS
    # Byte-for-byte: the serializer output serializes identically to the pre-Slice-3 shape.
    assert json.dumps(out) == json.dumps(reference)
    # No new-field key leaks even though the idea carries non-default new-field values.
    for leaked in ("operation_kind", "measure_refs", "grain_ref", "time_ref", "window",
                   "grouping_refs", "validation_status", "requirements"):
        assert leaked not in out


def test_v2_carries_the_new_fields():
    out = serialize_feature_idea_v2(_idea_with_new_fields())
    assert out["operation_kind"] == "avg"
    assert out["measure_refs"] == [["deposits", "public.accounts.balance"]]
    assert out["grain_ref"] == ["deposits", "public.accounts.id"]
    assert out["time_ref"] is None
    assert out["window"] == "30d"
    assert out["grouping_refs"] == [["deposits", "public.accounts.cust_id"]]
    assert out["validation_status"] == "NEEDS_EXTERNAL_VALIDATION"
    assert out["requirements"] == [
        {"code": "TYPE_IS_NUMERIC", "operand": ["deposits", "public.accounts.balance"], "detail": "verify"}]
    # v2 is a strict superset of v1 (same v1 keys, same values).
    v1 = serialize_feature_idea_v1(_idea_with_new_fields())
    for k, v in v1.items():
        assert out[k] == v


def test_dispatch_matches_flag():
    idea = _idea_with_new_fields()
    assert serialize_feature_idea(idea, feature_context=False) == serialize_feature_idea_v1(idea)
    assert serialize_feature_idea(idea, feature_context=True) == serialize_feature_idea_v2(idea)


def _recommend_fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a business column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance", "description": "average balance per customer",
            "derives_from": ["public.accounts.balance"],
            "aggregation": "avg", "grain_table": "customers"}]}),
    })


def test_recommend_response_has_no_new_field_markers_when_flag_off(make_client, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    client: TestClient = make_client(llm_client=_recommend_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/recommend",
                      json={"objective": "predict churn", "catalog_source": "deposits"},
                      headers=AUTH)
    assert res.status_code == 200
    proposals = res.json()["proposals"]
    assert len(proposals) == 1
    assert sorted(proposals[0].keys()) == sorted(_PRE_SLICE3_KEYS)
    # The new field names never appear anywhere in the raw response bytes.
    for marker in (b"validation_status", b"operation_kind", b"measure_refs", b"requirements",
                   b"grouping_refs"):
        assert marker not in res.content, marker
```

- [ ] Run it and expect FAIL (`featuregen.api.feature_serialize` does not exist; the route returns the raw dataclass with the new fields leaking):
```
.venv/bin/python -m pytest tests/featuregen/api/test_feature_serialize.py -q
```

- [ ] Create `src/featuregen/api/feature_serialize.py`:
```python
"""Explicit v1/v2 FeatureIdea response serializers (spec §8).

The assist routes must NOT return the shared FeatureIdea dataclass directly — any new field would
silently leak into the flag-OFF response (and, via the same shape, break the pre-Slice-3 contract).
v1 emits EXACTLY the pre-Slice-3 field set/order so a flag-OFF response is byte-identical; v2 adds the
Slice-3 fields. The flag is captured ONCE at the route and passed in as `feature_context`."""
from __future__ import annotations

from featuregen.overlay.upload.feature_assist import FeatureIdea, Requirement


def _pair(p: tuple[str, str] | None) -> list[str] | None:
    return list(p) if p is not None else None


def _req(r: Requirement) -> dict:
    return {"code": r.code, "operand": list(r.operand), "detail": r.detail}


def serialize_feature_idea_v1(idea: FeatureIdea) -> dict:
    """The pre-Slice-3 shape, in the dataclass field order FastAPI's jsonable_encoder produced.
    The Slice-3 fields are NEVER emitted — flag-OFF byte-identity depends on this."""
    return {
        "name": idea.name,
        "description": idea.description,
        "derives_from": list(idea.derives_from),
        "aggregation": idea.aggregation,
        "grain_table": idea.grain_table,
        "derives_pairs": [list(p) for p in idea.derives_pairs],
        "verification": idea.verification,
        "critic_note": idea.critic_note,
        "rationale": idea.rationale,
    }


def serialize_feature_idea_v2(idea: FeatureIdea) -> dict:
    """v1 plus the Slice-3 typed-computation + tri-state fields."""
    out = serialize_feature_idea_v1(idea)
    out["operation_kind"] = idea.operation_kind
    out["measure_refs"] = [list(m) for m in idea.measure_refs]
    out["grain_ref"] = _pair(idea.grain_ref)
    out["time_ref"] = _pair(idea.time_ref)
    out["window"] = idea.window
    out["grouping_refs"] = [list(g) for g in idea.grouping_refs]
    out["validation_status"] = idea.validation_status
    out["requirements"] = [_req(r) for r in idea.requirements]
    return out


def serialize_feature_idea(idea: FeatureIdea, *, feature_context: bool) -> dict:
    return serialize_feature_idea_v2(idea) if feature_context else serialize_feature_idea_v1(idea)
```

- [ ] Wire the serializers into `src/featuregen/api/routes/assist.py`. Add the imports at the top of the module import block:
```python
from featuregen.api.feature_serialize import serialize_feature_idea
from featuregen.overlay.upload.feature_assist import feature_context_enabled
```
  In `recommend`, capture the flag once and serialize proposals — change the trailing `return`:
```python
    feature_context = feature_context_enabled()
    return {"proposals": [serialize_feature_idea(i, feature_context=feature_context)
                          for i in report.ideas],
            "rejections": report.rejections}
```
  In `refine`, serialize the revised idea:
```python
    feature_context = feature_context_enabled()
    if revised is not None:
        return {"revised": serialize_feature_idea(revised, feature_context=feature_context)}
    rej = rejection or {}
    return {"rejected": {"reason": str(rej.get("reason", "")), "code": str(rej.get("code", ""))}}
```
  In `recommend_sets`, serialize each set's features (keeps the flag-OFF `{"lens","features"}` shape byte-identical):
```python
    feature_context = feature_context_enabled()
    sets = [{"lens": s.lens,
             "features": [serialize_feature_idea(f, feature_context=feature_context)
                          for f in s.features]}
            for s in report.sets]
    recommendation = (recommend_set(conn, report.sets, body.objective, client, actor=identity)
                      if any(s.features for s in report.sets) else None)
    return {"sets": sets, "recommendation": recommendation, "rejections": report.rejections}
```

- [ ] Run the test and expect PASS:
```
.venv/bin/python -m pytest tests/featuregen/api/test_feature_serialize.py -q
```

- [ ] Run the existing assist route suite to confirm the flag-OFF responses still match its assertions (byte-identity regression guard):
```
.venv/bin/python -m pytest tests/featuregen/api/test_feature_assist.py -q
```

- [ ] Lint, then commit:
```
.venv/bin/python -m ruff check src/featuregen/api/feature_serialize.py src/featuregen/api/routes/assist.py tests/featuregen/api/test_feature_serialize.py
git add -A && git commit -m "feat(3A-iv): explicit v1/v2 FeatureIdea serializers + flag capture at assist routes"
```

---

## Task 4 — Flag-gate the Gate #1 snapshot serializer (considered-set byte-identity)

**Files:**
- Modify: `src/featuregen/overlay/upload/contract/gate1.py` (`_idea_json`)
- Test: `tests/featuregen/overlay/upload/contract/test_gate1_snapshot_byte_identity.py` (create)

**Interfaces:**
- Consumes: `feature_context_enabled` from `featuregen.overlay.upload.feature_assist`; `FeatureIdea`, `Requirement`; the `_idea_json(f: FeatureIdea | None) -> dict | None` that 3A-ii extended to emit `validation_status` + `requirements` unconditionally.
- Produces: `_idea_json` emits the pre-Slice-3 snapshot keys (`name, derives_from, aggregation, grain_table, verification, critic_note, rationale, derives_pairs`) ALWAYS, and appends `validation_status` + `requirements` ONLY when `feature_context_enabled()`. Flag-OFF snapshot is byte-identical to pre-Slice-3; `_idea_from_json` (3A-ii) still restores them when present. The paired restore is unaffected (a missing key falls back to the dataclass default, exactly as pre-Slice-3).

Steps:

- [ ] Write the failing test `tests/featuregen/overlay/upload/contract/test_gate1_snapshot_byte_identity.py`:
```python
"""Slice 3A-iv Task 4: the considered-set snapshot (`_idea_json`) is flag-gated.

Flag-OFF, the snapshot is BYTE-IDENTICAL to pre-Slice-3 even when the idea carries non-default
Slice-3 fields; flag-ON, it additionally carries validation_status + requirements (spec §8)."""
from __future__ import annotations

import json

from featuregen.overlay.upload.contract.gate1 import _idea_json
from featuregen.overlay.upload.feature_assist import FeatureIdea, Requirement

# The exact key order `_idea_json` produced pre-Slice-3 (gate1.py: name, derives_from, aggregation,
# grain_table, verification, critic_note, rationale, derives_pairs). NOTE: `description` is omitted
# and `derives_pairs` is LAST — this differs from the route serializer's dataclass order.
_PRE_SLICE3_SNAPSHOT_KEYS = ["name", "derives_from", "aggregation", "grain_table", "verification",
                             "critic_note", "rationale", "derives_pairs"]


def _idea_with_new_fields() -> FeatureIdea:
    return FeatureIdea(
        name="avg_balance", description="average balance", derives_from=["public.accounts.balance"],
        aggregation="avg", grain_table="accounts",
        derives_pairs=(("deposits", "public.accounts.balance"),),
        verification="DESIGN-CHECKED", critic_note="", rationale="why",
        operation_kind="avg", measure_refs=(("deposits", "public.accounts.balance"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("deposits", "public.accounts.balance"), "verify"),))


def test_none_snapshots_to_none():
    assert _idea_json(None) is None


def test_snapshot_byte_identical_when_flag_off(monkeypatch):
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    reference = {
        "name": "avg_balance", "derives_from": ["public.accounts.balance"], "aggregation": "avg",
        "grain_table": "accounts", "verification": "DESIGN-CHECKED", "critic_note": "",
        "rationale": "why", "derives_pairs": [["deposits", "public.accounts.balance"]],
    }
    out = _idea_json(_idea_with_new_fields())
    assert list(out.keys()) == _PRE_SLICE3_SNAPSHOT_KEYS
    assert json.dumps(out) == json.dumps(reference)
    assert "validation_status" not in out
    assert "requirements" not in out


def test_snapshot_carries_new_fields_when_flag_on(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    out = _idea_json(_idea_with_new_fields())
    assert out["validation_status"] == "NEEDS_EXTERNAL_VALIDATION"
    assert out["requirements"] == [
        {"code": "TYPE_IS_NUMERIC", "operand": ["deposits", "public.accounts.balance"], "detail": "verify"}]
    # The pre-Slice-3 keys still lead, in the same order.
    assert list(out.keys())[:len(_PRE_SLICE3_SNAPSHOT_KEYS)] == _PRE_SLICE3_SNAPSHOT_KEYS
```

- [ ] Run it and expect FAIL (3A-ii's `_idea_json` emits `validation_status`/`requirements` unconditionally, so the flag-OFF byte-identity assertion fails):
```
.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_gate1_snapshot_byte_identity.py -q
```

- [ ] Implement in `gate1.py`. Add the import to the top-of-module import block:
```python
from featuregen.overlay.upload.feature_assist import feature_context_enabled
```
  Locate `_idea_json` (find by symbol `def _idea_json(`). Replace its body so the two Slice-3 keys (added by 3A-ii) are gated behind the flag — build the pre-Slice-3 dict first, then conditionally append:
```python
def _idea_json(f: FeatureIdea | None) -> dict | None:
    if f is None:
        return None
    out = {"name": f.name, "derives_from": f.derives_from, "aggregation": f.aggregation,
           "grain_table": f.grain_table,   # keep grain — it disambiguates same-named options
           "verification": f.verification,   # honest §14.5 stamp surfaced at Gate #1 (item 4)
           "critic_note": f.critic_note,     # advisory residual critic note — the human weighs it
           "rationale": f.rationale,         # §14.2 one-line causal 'why' — audit the logic first
           "derives_pairs": [list(p) for p in f.derives_pairs]}  # for server-side reconstruction
    # Slice-3A-iv: the tri-state fields ride the snapshot ONLY when the feature-context flag is on, so
    # a flag-OFF snapshot is byte-identical to pre-Slice-3. `_idea_from_json` restores them when present
    # and falls back to the dataclass defaults when absent (exactly the pre-Slice-3 behavior).
    if feature_context_enabled():
        out["validation_status"] = f.validation_status
        out["requirements"] = [{"code": r.code, "operand": list(r.operand), "detail": r.detail}
                               for r in f.requirements]
    return out
```
  NOTE (verify by symbol before editing): if 3A-ii added the two keys inside the initial dict literal rather than as trailing statements, remove them from the literal and keep only the flag-gated block above.

- [ ] Run the test and expect PASS:
```
.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_gate1_snapshot_byte_identity.py -q
```

- [ ] Run the Gate #1 / contract snapshot round-trip suites to confirm the flag-ON restore path (3A-ii) and confirm_gate1 still work:
```
.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_gate1.py tests/featuregen/overlay/upload/contract/test_author.py -q
```

- [ ] Lint, then commit:
```
.venv/bin/python -m ruff check src/featuregen/overlay/upload/contract/gate1.py tests/featuregen/overlay/upload/contract/test_gate1_snapshot_byte_identity.py
git add -A && git commit -m "feat(3A-iv): flag-gate Gate #1 snapshot serializer for considered-set byte-identity"
```

---

## Task 5 — Quality-gate hermetic core: gold set (≥40) + metric module + CI-gate units

**Files:**
- Create: `tests/eval/gold_features.py` (the versioned gold artifact)
- Create: `tests/eval/feature_eval.py` (pure metric functions)
- Create: `tests/eval/test_feature_eval.py` (UNMARKED hermetic CI gate — metric units + gold invariants)

**Interfaces:**
- Consumes: nothing at runtime (pure Python + the artifact). No DB, no SDK — this is the CI gate that runs by default.
- Produces:
  - `GoldFeature(objective, entity, catalog_source, expected_columns, expected_operations, expected_disposition, relevance_terms)`; `GOLD: list[GoldFeature]` (≥40, versioned); `OPERATION_VOCAB`, `DISPOSITIONS`.
  - `feature_eval.GenFeature(name, derives_from, operation_kind, validation_status, requirement_count)` and the pure metrics: `is_relevant`, `relevance_rate`, `relevance_lift`, `unsafe_accepted`, `token_total`, `cost_regression`, `restricted_leaks`.
  - The delivery-bar semantics (spec §9): `unsafe_accepted` = features with `validation_status == "DESIGN_CHECKED"` yet a non-empty requirement set (a DESIGN_CHECKED must never carry an unresolved requirement); `relevance_lift` (relative); `cost_regression` (relative token regression); `restricted_leaks` (sentinel scan over recorded egress payloads).

Steps:

- [ ] Create the metric module `tests/eval/feature_eval.py` (write it first — the gold-set test imports it):
```python
"""Pure, hermetic metrics for the Slice-3 feature-gen quality gate (spec §9).

DB-free and SDK-free on purpose: the metric logic is a CI gate (test_feature_eval.py) even though the
key-gated eval that consumes it (test_feature_gen_eval.py) only runs with a live provider key."""
from __future__ import annotations

from dataclasses import dataclass


def _tokens(text: str) -> set[str]:
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    return {t for t in cleaned.split() if len(t) > 2}


@dataclass(frozen=True, slots=True)
class GenFeature:
    """The eval's transport-agnostic view of one generated feature."""
    name: str
    derives_from: tuple[str, ...]
    operation_kind: str
    validation_status: str
    requirement_count: int


def is_relevant(gen: GenFeature, expected_columns: frozenset[str],
                relevance_terms: frozenset[str]) -> bool:
    """Objective (no LLM judge): a feature is relevant if it derives from an expert-expected column,
    or its name shares a relevance term with the objective."""
    if any(ref in expected_columns for ref in gen.derives_from):
        return True
    return bool(_tokens(gen.name) & relevance_terms)


def relevance_rate(gens: list[GenFeature], expected_columns: frozenset[str],
                   relevance_terms: frozenset[str]) -> float:
    if not gens:
        return 0.0
    hits = sum(1 for g in gens if is_relevant(g, expected_columns, relevance_terms))
    return hits / len(gens)


def relevance_lift(baseline_rate: float, enriched_rate: float) -> float:
    """Relative lift of enriched over baseline. A zero baseline with any enriched hits is unbounded
    improvement (inf); zero over zero is no change (0.0)."""
    if baseline_rate <= 0.0:
        return float("inf") if enriched_rate > 0.0 else 0.0
    return (enriched_rate - baseline_rate) / baseline_rate


def unsafe_accepted(gens: list[GenFeature]) -> list[GenFeature]:
    """The hard-safety bar: a DESIGN_CHECKED feature must NEVER carry an unresolved requirement — that
    is exactly the NEEDS_EXTERNAL_VALIDATION contract. Any such feature is an unsafe acceptance."""
    return [g for g in gens
            if g.validation_status == "DESIGN_CHECKED" and g.requirement_count > 0]


def token_total(cost_metadata: dict) -> int:
    """Sum whatever input/output token counts the recorded cost_metadata carries; absent -> 0."""
    return int(cost_metadata.get("input_tokens", 0) or 0) + \
        int(cost_metadata.get("output_tokens", 0) or 0)


def cost_regression(baseline_tokens: int, enriched_tokens: int) -> float:
    """Relative token regression of enriched over baseline (0.25 == +25%)."""
    if baseline_tokens <= 0:
        return 0.0 if enriched_tokens <= 0 else float("inf")
    return (enriched_tokens - baseline_tokens) / baseline_tokens


def restricted_leaks(payloads: list[str], sentinels: frozenset[str]) -> list[str]:
    """Sentinels (seeded sample/PII markers) that survived into any recorded egress payload."""
    return [s for s in sentinels if any(s in p for p in payloads)]
```

- [ ] Create the versioned gold artifact `tests/eval/gold_features.py`. Provide the dataclass, vocab constants, and the curated cases. **The `GOLD` list must contain ≥ 40 entries** (the invariant test in this task is the hard gate). Seed with the concrete, distinct cases below and expand to ≥ 40 following the identical shape — every case anchored to real bank-catalog object_refs, spanning the hard families (aggregation over children, ratio, recency/temporal, distributional, unary, cross-entity):
```python
"""Curated gold set for the Slice-3 feature-gen quality gate (spec §9): objective -> expert-expected
feature. A VERSIONED artifact — grow it as reviewers adjudicate more objectives; keep >= 40 cases.

INVARIANTS (enforced by test_feature_eval.py):
- len(GOLD) >= 40
- every `expected_operations` is a non-empty subset of OPERATION_VOCAB
- every `expected_disposition` is in DISPOSITIONS
- every `relevance_terms` and `expected_columns` is non-empty
- objectives are unique (no duplicate case)

`expected_columns` are object_refs an expert feature would derive from; `relevance_terms` are lowercased
objective anchors the scorer credits a feature name against; `expected_disposition` is the honest state
an expert expects for that feature given a governed catalog (numeric ops on FTR-style unknown-operational
measures land in NEEDS_EXTERNAL_VALIDATION; confirmed-grain counts land in DESIGN_CHECKED)."""
from __future__ import annotations

from dataclasses import dataclass

OPERATION_VOCAB = frozenset({
    "sum", "count", "count_distinct", "avg", "ratio", "recency", "min", "max", "stddev", "trend",
})
DISPOSITIONS = frozenset({"DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION"})


@dataclass(frozen=True, slots=True)
class GoldFeature:
    objective: str
    entity: str | None
    catalog_source: str | None
    expected_columns: frozenset[str]
    expected_operations: frozenset[str]
    expected_disposition: str
    relevance_terms: frozenset[str]


GOLD: list[GoldFeature] = [
    GoldFeature("predict account churn from spending drop", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"sum", "avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"spending", "spend", "churn", "amount"})),
    GoldFeature("count transactions per account in the last 90 days", "Account", "bank",
                frozenset({"public.transactions.txn_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"transactions", "count", "account"})),
    GoldFeature("recency of last transaction per account", "Account", "bank",
                frozenset({"public.transactions.txn_date"}), frozenset({"recency"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"recency", "last", "transaction"})),
    GoldFeature("distinct merchants a customer transacted with", "Customer", "bank",
                frozenset({"public.transactions.merchant_id"}), frozenset({"count_distinct"}),
                "DESIGN_CHECKED", frozenset({"distinct", "merchants", "customer"})),
    GoldFeature("ratio of debit to credit volume per account", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"ratio"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"ratio", "debit", "credit", "volume"})),
    GoldFeature("average balance held per customer", "Customer", "bank",
                frozenset({"public.accounts.balance"}), frozenset({"avg"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"average", "balance", "customer"})),
    GoldFeature("total loan exposure per customer", "Customer", "bank",
                frozenset({"public.loans.principal"}), frozenset({"sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"loan", "exposure", "total"})),
    GoldFeature("days past due trend per loan", "Loan", "bank",
                frozenset({"public.loans.dpd"}), frozenset({"trend", "max"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"days", "past", "due", "trend", "loan"})),
    GoldFeature("count of declined card authorizations per account", "Account", "bank",
                frozenset({"public.card_auth.auth_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"declined", "authorizations", "card"})),
    GoldFeature("standard deviation of transaction amount per account", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"stddev"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"deviation", "transaction", "amount"})),
    GoldFeature("recency of last login per customer", "Customer", "bank",
                frozenset({"public.sessions.login_at"}), frozenset({"recency"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"recency", "last", "login"})),
    GoldFeature("count of distinct product holdings per customer", "Customer", "bank",
                frozenset({"public.holdings.product_id"}), frozenset({"count_distinct"}),
                "DESIGN_CHECKED", frozenset({"distinct", "product", "holdings"})),
    GoldFeature("maximum single transaction amount per account", "Account", "bank",
                frozenset({"public.transactions.amount"}), frozenset({"max"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"maximum", "transaction", "amount"})),
    GoldFeature("minimum balance over the period per account", "Account", "bank",
                frozenset({"public.accounts.balance"}), frozenset({"min"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"minimum", "balance", "period"})),
    GoldFeature("count of active accounts per customer", "Customer", "bank",
                frozenset({"public.accounts.account_id"}), frozenset({"count"}),
                "DESIGN_CHECKED", frozenset({"active", "accounts", "customer"})),
    GoldFeature("total fees charged per account", "Account", "bank",
                frozenset({"public.transactions.fee"}), frozenset({"sum"}),
                "NEEDS_EXTERNAL_VALIDATION", frozenset({"total", "fees", "charged"})),
    # --- BUILD INSTRUCTION (test-enforced, NOT optional): add >= 25 more GoldFeature cases in the
    # identical shape until len(GOLD) >= 40, covering: additional temporal/recency objectives, more
    # ratio/utilization objectives, distributional (z-score/percentile) objectives, unary transforms,
    # and cross-catalog/cross-entity objectives. Anchor every expected_columns to a real object_ref;
    # keep objectives unique. test_feature_eval.py::test_gold_has_at_least_40 fails until this is done.
]
```

- [ ] Write the hermetic CI-gate test `tests/eval/test_feature_eval.py` (UNMARKED, so default CI runs it):
```python
"""Slice 3A-iv Task 5: hermetic CI gate for the feature-gen quality metrics + the gold artifact.

Deliberately UNMARKED (no `eval` marker) and DB-free / SDK-free: this is the always-on gate, even
though the sweep that consumes these metrics (test_feature_gen_eval.py) only runs with a live key."""
from __future__ import annotations

from tests.eval.feature_eval import (
    GenFeature,
    cost_regression,
    is_relevant,
    relevance_lift,
    relevance_rate,
    restricted_leaks,
    token_total,
    unsafe_accepted,
)
from tests.eval.gold_features import DISPOSITIONS, GOLD, OPERATION_VOCAB


# ---- metric units -------------------------------------------------------------------------------

def _gen(name, derives, op="", status="DESIGN_CHECKED", reqs=0):
    return GenFeature(name=name, derives_from=tuple(derives), operation_kind=op,
                      validation_status=status, requirement_count=reqs)


def test_is_relevant_by_column_match():
    g = _gen("some_feature", ["public.transactions.amount"])
    assert is_relevant(g, frozenset({"public.transactions.amount"}), frozenset()) is True


def test_is_relevant_by_name_term():
    g = _gen("spend_drop_90d", [])
    assert is_relevant(g, frozenset(), frozenset({"spend"})) is True


def test_is_not_relevant_when_neither_matches():
    g = _gen("balance_avg", ["public.other.col"])
    assert is_relevant(g, frozenset({"public.transactions.amount"}), frozenset({"spend"})) is False


def test_relevance_rate_and_empty():
    gens = [_gen("spend_x", ["public.transactions.amount"]), _gen("noise", ["public.other.col"])]
    assert relevance_rate(gens, frozenset({"public.transactions.amount"}), frozenset()) == 0.5
    assert relevance_rate([], frozenset({"x"}), frozenset({"y"})) == 0.0


def test_relevance_lift_relative_and_edge_cases():
    assert relevance_lift(0.4, 0.5) == (0.5 - 0.4) / 0.4
    assert relevance_lift(0.0, 0.3) == float("inf")
    assert relevance_lift(0.0, 0.0) == 0.0


def test_unsafe_accepted_flags_design_checked_with_requirements():
    safe = _gen("a", ["c"], status="DESIGN_CHECKED", reqs=0)
    needs = _gen("b", ["c"], status="NEEDS_EXTERNAL_VALIDATION", reqs=1)
    unsafe = _gen("c", ["c"], status="DESIGN_CHECKED", reqs=1)
    result = unsafe_accepted([safe, needs, unsafe])
    assert result == [unsafe]


def test_token_total_and_cost_regression():
    assert token_total({"input_tokens": 100, "output_tokens": 40}) == 140
    assert token_total({}) == 0
    assert cost_regression(1000, 1200) == 0.2
    assert cost_regression(0, 0) == 0.0
    assert cost_regression(0, 5) == float("inf")


def test_restricted_leaks_finds_seeded_sentinel():
    payloads = ["clean context", "leaked SAMPLE:jane@acme.com here"]
    assert restricted_leaks(payloads, frozenset({"SAMPLE:jane@acme.com"})) == ["SAMPLE:jane@acme.com"]
    assert restricted_leaks(["clean"], frozenset({"SAMPLE:jane@acme.com"})) == []


# ---- gold-set invariants (the ">= 40 curated cases" gate) ---------------------------------------

def test_gold_has_at_least_40():
    assert len(GOLD) >= 40, f"gold set has only {len(GOLD)} cases; spec §9 requires >= 40"


def test_gold_objectives_are_unique():
    objectives = [g.objective for g in GOLD]
    assert len(objectives) == len(set(objectives)), "duplicate objective in the gold set"


def test_gold_operations_in_vocab_and_nonempty():
    for g in GOLD:
        assert g.expected_operations, f"{g.objective!r} has no expected_operations"
        assert g.expected_operations <= OPERATION_VOCAB, \
            f"{g.objective!r} uses off-vocab operations {g.expected_operations - OPERATION_VOCAB}"


def test_gold_dispositions_and_anchors_valid():
    for g in GOLD:
        assert g.expected_disposition in DISPOSITIONS, g.expected_disposition
        assert g.expected_columns, f"{g.objective!r} has no expected_columns"
        assert g.relevance_terms, f"{g.objective!r} has no relevance_terms"
```

- [ ] Run the hermetic gate. It FAILS initially on `test_gold_has_at_least_40` (only 16 seed cases). This failure is the enforcement mechanism:
```
.venv/bin/python -m pytest tests/eval/test_feature_eval.py -q
```

- [ ] Expand `tests/eval/gold_features.py` to ≥ 40 `GoldFeature` cases following the BUILD INSTRUCTION shape (unique objectives; real object_refs; vocab operations). Re-run and expect PASS:
```
.venv/bin/python -m pytest tests/eval/test_feature_eval.py -q
```

- [ ] Lint, then commit:
```
.venv/bin/python -m ruff check tests/eval/feature_eval.py tests/eval/gold_features.py tests/eval/test_feature_eval.py
git add -A && git commit -m "feat(3A-iv): quality-gate hermetic core — gold set (>=40) + metric module + CI-gate units"
```

---

## Task 6 — Key-gated real-provider baseline-vs-enriched eval + versioned report + runnable command

**Files:**
- Create: `tests/eval/test_feature_gen_eval.py` (marked `eval`, skips without a key)
- (Reports land under `tests/eval/reports/` — created at runtime; no source change.)

**Interfaces:**
- Consumes: `build_claude_llm`, `ClaudeConfig`, `PROVIDER_OK` (via `featuregen.intake.llm_claude` / `featuregen.intake.llm`); `DEFAULT_LLM_MODEL`; `recommend_features_report(conn, objective, client, *, catalog_source, entity, roles, now, actor)` from `feature_assist`; `build_graph`; `CanonicalRow`; the `db` fixture (rolled-back real conn) from `tests/eval/conftest.py`; the metric module + gold set from Task 5; `feature_context_enabled`.
- Produces: one manually-run, versioned evaluation that runs each gold objective twice against the PINNED real provider — baseline (`FEATUREGEN_FEATURE_CONTEXT=0`, thin menu) and enriched (`=1`, widened menu + tri-state) — computes the spec §9 metrics, writes a versioned report to `tests/eval/reports/`, and asserts the delivery bars: **zero** unsafe-accepted, **zero** unsanitized outbound sentinels, grounded-acceptance non-regression, **≥ 15%** relative relevance lift, **≤ 25%** token/cost regression, with the pinned model/settings recorded in the report. The `anthropic` provider is selected via env (`FEATUREGEN_LLM_PROVIDER=anthropic`); the model is read from `FEATUREGEN_LLM_MODEL` (default `DEFAULT_LLM_MODEL`) and RECORDED — the plan pins by recording the exact model+settings, never by hardcoding a model literal.

Steps:

- [ ] Write the key-gated eval `tests/eval/test_feature_gen_eval.py`:
```python
"""Slice 3A-iv Task 6 — key-gated real-provider feature-gen quality gate (spec §9).

Runs each gold objective twice against the PINNED provider: baseline (thin menu, flag off) vs enriched
(widened menu + tri-state, flag on). Computes the §9 metrics, writes a versioned report under
tests/eval/reports/, then asserts the delivery bars. A key-gated test that merely SKIPS is not itself
the gate (that is the hermetic core in test_feature_eval.py + the byte-identity units); THIS run is the
manual, versioned evidence.

Run it (needs a live key; skips cleanly without one):

    FEATUREGEN_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... \
        .venv/bin/python -m pytest -m eval tests/eval/test_feature_gen_eval.py -q -s
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from featuregen.intake.llm import DEFAULT_LLM_MODEL
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import recommend_features_report
from featuregen.overlay.upload.graph import build_graph
from tests.eval.feature_eval import (
    GenFeature,
    cost_regression,
    relevance_lift,
    relevance_rate,
    restricted_leaks,
    token_total,
    unsafe_accepted,
)
from tests.eval.gold_features import GOLD

pytestmark = pytest.mark.eval

# A seeded sample/PII sentinel embedded in a column definition — it must NEVER reach the provider
# (the nested field-aware egress adapter from 3A-iii sanitizes definition-kind fields before dispatch).
_SENTINEL = "SAMPLE:jane.doe@acme-bank.example"
_SENTINELS = frozenset({_SENTINEL})

# Delivery bars (spec §9).
_MIN_RELEVANCE_LIFT = 0.15      # >= 15% relative
_MAX_COST_REGRESSION = 0.25     # <= 25%

_BANK_ROWS = [
    CanonicalRow("bank", "transactions", "acct_id", "integer",
                 joins_to="accounts.account_id", cardinality="N:1"),
    CanonicalRow("bank", "transactions", "txn_id", "integer", is_grain=True, entity="Transaction"),
    CanonicalRow("bank", "transactions", "amount", "numeric",
                 definition=f"signed transaction amount (e.g. {_SENTINEL})",
                 additivity="additive", unit="dollars", currency="USD", entity="Transaction"),
    CanonicalRow("bank", "transactions", "merchant_id", "integer", entity="Merchant"),
    CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),
    CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True, entity="Account"),
    CanonicalRow("bank", "accounts", "balance", "numeric", definition="end-of-day ledger balance",
                 additivity="semi_additive", unit="dollars", currency="USD", entity="Account"),
    CanonicalRow("bank", "accounts", "cust_id", "integer",
                 joins_to="customers.cust_id", cardinality="N:1", entity="Customer"),
    CanonicalRow("bank", "customers", "cust_id", "integer", is_grain=True, entity="Customer"),
    CanonicalRow("bank", "loans", "loan_id", "integer", is_grain=True, entity="Loan"),
    CanonicalRow("bank", "loans", "principal", "numeric", definition="loan principal outstanding",
                 additivity="additive", unit="dollars", currency="USD", entity="Loan"),
]


def _gens(report) -> list[GenFeature]:
    return [GenFeature(name=i.name, derives_from=tuple(i.derives_from),
                       operation_kind=i.operation_kind, validation_status=i.validation_status,
                       requirement_count=len(i.requirements))
            for i in report.ideas]


def _egress_payloads(db) -> list[str]:
    rows = db.execute("SELECT redacted_input FROM llm_call "
                      "WHERE task LIKE 'overlay.feature.%'").fetchall()
    return [json.dumps(r[0]) for r in rows if r[0] is not None]


def _run(db, client, objective, entity, *, feature_context: bool):
    if feature_context:
        os.environ["FEATUREGEN_FEATURE_CONTEXT"] = "1"
    else:
        os.environ.pop("FEATUREGEN_FEATURE_CONTEXT", None)
    return recommend_features_report(db, objective, client, catalog_source="bank", entity=entity,
                                     roles=("platform_admin",), now=datetime.now(UTC), critic=False)


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                    reason="live provider eval; set ANTHROPIC_API_KEY to run")
def test_feature_gen_baseline_vs_enriched(db):
    from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm
    os.environ.setdefault("FEATUREGEN_LLM_PROVIDER", "anthropic")
    model = os.environ.get("FEATUREGEN_LLM_MODEL", DEFAULT_LLM_MODEL)
    client = build_claude_llm(ClaudeConfig(enabled=True, model=model))

    build_graph(db, "bank", _BANK_ROWS)

    per_case: list[dict] = []
    base_rates: list[float] = []
    enr_rates: list[float] = []
    base_tokens = 0
    enr_tokens = 0
    unsafe: list[str] = []
    leaks: set[str] = set()

    for g in GOLD:
        if g.catalog_source != "bank":
            continue   # this fixture is the 'bank' catalog; skip cases anchored elsewhere
        before = {r[0] for r in db.execute("SELECT llm_call_ref FROM llm_call").fetchall()}
        base = _run(db, client, g.objective, g.entity, feature_context=False)
        mid = {r[0] for r in db.execute("SELECT llm_call_ref FROM llm_call").fetchall()}
        enr = _run(db, client, g.objective, g.entity, feature_context=True)
        after = {r[0] for r in db.execute("SELECT llm_call_ref FROM llm_call").fetchall()}

        base_gens, enr_gens = _gens(base), _gens(enr)
        br = relevance_rate(base_gens, g.expected_columns, g.relevance_terms)
        er = relevance_rate(enr_gens, g.expected_columns, g.relevance_terms)
        base_rates.append(br)
        enr_rates.append(er)

        base_tokens += _tokens_for(db, mid - before)
        enr_tokens += _tokens_for(db, after - mid)

        unsafe += [f"{g.objective}:{f.name}" for f in unsafe_accepted(enr_gens)]
        leaks |= set(restricted_leaks(_egress_payloads(db), _SENTINELS))

        per_case.append({"objective": g.objective, "baseline_relevance": round(br, 3),
                         "enriched_relevance": round(er, 3),
                         "baseline_features": len(base_gens), "enriched_features": len(enr_gens)})

    n = len(base_rates)
    assert n > 0, "no 'bank' gold cases were exercised"
    mean_base = sum(base_rates) / n
    mean_enr = sum(enr_rates) / n
    lift = relevance_lift(mean_base, mean_enr)
    cost_reg = cost_regression(base_tokens, enr_tokens)
    base_accept = sum(c["baseline_features"] for c in per_case)
    enr_accept = sum(c["enriched_features"] for c in per_case)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "provider": os.environ.get("FEATUREGEN_LLM_PROVIDER"),
        "settings": {"critic": False, "roles": ["platform_admin"]},
        "gold_cases_exercised": n,
        "mean_baseline_relevance": round(mean_base, 4),
        "mean_enriched_relevance": round(mean_enr, 4),
        "relevance_lift": None if lift == float("inf") else round(lift, 4),
        "baseline_accepted": base_accept,
        "enriched_accepted": enr_accept,
        "baseline_tokens": base_tokens,
        "enriched_tokens": enr_tokens,
        "cost_regression": None if cost_reg == float("inf") else round(cost_reg, 4),
        "unsafe_accepted": unsafe,
        "restricted_leaks": sorted(leaks),
        "per_case": per_case,
    }

    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = reports_dir / f"feature_gen_eval_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n" + json.dumps(report, indent=2))   # visible with -s

    # Delivery bars (spec §9) — assert AFTER writing the report so a failure still leaves diagnostics.
    assert report_path.exists()
    assert unsafe == [], f"unsafe-accepted features (DESIGN_CHECKED with requirements): {unsafe}"
    assert sorted(leaks) == [], f"restricted/sample fields egressed unsanitized: {sorted(leaks)}"
    assert enr_accept >= base_accept, \
        f"grounded-acceptance regressed: enriched {enr_accept} < baseline {base_accept}"
    assert lift >= _MIN_RELEVANCE_LIFT, \
        f"relevance lift {lift:.3f} < required {_MIN_RELEVANCE_LIFT} (see {report_path})"
    assert cost_reg <= _MAX_COST_REGRESSION, \
        f"cost regression {cost_reg:.3f} > allowed {_MAX_COST_REGRESSION} (see {report_path})"


def _tokens_for(db, refs: set[str]) -> int:
    if not refs:
        return 0
    rows = db.execute("SELECT cost_metadata FROM llm_call WHERE llm_call_ref = ANY(%s)",
                      (list(refs),)).fetchall()
    return sum(token_total(r[0]) for r in rows if r[0] is not None)
```

- [ ] Confirm the eval SKIPS cleanly WITHOUT a key (this is the only behavior default CI / a keyless env sees). Because it is `eval`-marked, invoke it with `-m eval`:
```
.venv/bin/python -m pytest -m eval tests/eval/test_feature_gen_eval.py -q
```
  Expect: `1 skipped` (skipif on `ANTHROPIC_API_KEY`). If a key IS present in the environment, expect a live run that writes a report under `tests/eval/reports/`.

- [ ] Confirm the eval is EXCLUDED from a default (keyless CI) run — the hermetic core still runs, the live eval does not collect:
```
.venv/bin/python -m pytest tests/eval/test_feature_eval.py -q
.venv/bin/python -m pytest tests/eval/ -q   # default addopts '-m not eval' -> the live eval is not collected
```

- [ ] Lint, then commit:
```
.venv/bin/python -m ruff check tests/eval/test_feature_gen_eval.py
git add -A && git commit -m "feat(3A-iv): key-gated real-provider feature-gen quality gate + versioned report"
```

---

## Self-Review

**Spec coverage (spec §8 + §9):**
- §8 v2 schemas: Task 1 registers `("feature_ideas",2)` + `feature_recipe`/`leakage`/`feature_set_rec` v2 as v1 aliases (permissive; semantic validation stays code-side in `_validate_idea`). ✔
- §8 threaded versions: Task 2 adds `prompt_version`/`schema_version` to `_call_raw`, threads to `audited_structured_call`, passes at all 7 call sites (6 feature-schema sites carry `_feature_schema_version()` = 2 when flagged; the critique site stays v1 — no `feature_candidate_critique` v2 is registered). ✔
- §8 v1/v2 serializers + flag: Task 3 adds explicit v1/v2 `FeatureIdea` serializers at the assist routes; the flag `FEATUREGEN_FEATURE_CONTEXT` (default off) is captured once at the route via `feature_context_enabled()`. ✔
- §8 byte-identity: Task 3 tests recommend-response byte-identity + no-leak; Task 4 flag-gates `_idea_json` and tests considered-set snapshot byte-identity. Both assert `json.dumps` byte equality against an explicit pre-Slice-3 reference and that the new fields do not leak when off. ✔
- §9 gold set ≥40 (versioned, under `tests/eval/`): Task 5 `gold_features.py` + a test-enforced `>= 40` invariant. ✔
- §9 runnable command: Task 6 docstring gives the exact `-m eval` command; the eval writes a versioned report to `tests/eval/reports/`. ✔
- §9 key-gated real-provider baseline-vs-enriched eval with the exact bars (zero unsafe-accepted; zero unsanitized outbound; grounded-acceptance non-regression; ≥15% relative lift; ≤25% cost regression; pinned model/settings recorded): Task 6. ✔
- §9 CI gates = hermetic tests + byte-identity + a contamination/threshold unit; the key-gated eval SKIPS without a key: Task 5 (`test_feature_eval.py`, UNMARKED) + Task 3/Task 4 byte-identity units are the CI gates; Task 6 is `eval`-marked and skipif-gated. ✔

**Placeholder scan:** No `...` in any test or implementation body. The single intentionally-curated artifact is the ≥40-case `GOLD` list — the plan ships 16 concrete cases plus a test-enforced BUILD INSTRUCTION and the `test_gold_has_at_least_40` gate that FAILS until the curation reaches 40, so the requirement is enforced in code, not left as a prose placeholder.

**Type consistency vs the shared contract:** Uses the exact shared names — `FeatureIdea` (with the 3A-i-added fields), `Requirement(code, operand, detail)`, `VALIDATION_STATES` values `DESIGN_CHECKED`/`NEEDS_EXTERNAL_VALIDATION` (not the hyphenated `verification` stamp, which is preserved as a separate axis and emitted verbatim by the v1 serializer), `feature_context_enabled` reading `FEATUREGEN_FEATURE_CONTEXT`, `_call_raw(..., prompt_version=1, schema_version=1)` threaded to `audited_structured_call`, and the v2 schema ids `feature_ideas`/`feature_recipe`/`leakage`/`feature_set_rec`. The requirement serialization `{"code","operand","detail"}` is identical in the route v2 serializer and the snapshot serializer.

**Ambiguities / concerns where spec/contract are underspecified for this area:**
1. **Which sites get v2 vs stay v1.** The shared contract says "pass at all 7 call sites" but §8 registers only 4 feature schemas (no `feature_candidate_critique` v2). This plan resolves it by threading the version to all 7 `_call_raw` sites while the critique site keeps the default 1 (no v2 registered) and the 6 feature-schema sites carry `2 when flagged`. If the intent was that `leakage`/`recipe`/`feature_set` remain v1 even when the flag is on (their inputs don't change under menu-widening the way `feature_ideas` does), the `_feature_schema_version()` call at those three sites should be replaced by a literal `1`. Flagged for the reviewer; the current reading treats v2 as "the Slice-3 regime egressed," which matches §8's stated rationale ("records the real numeric version instead of a hardcoded 1").
2. **"Captured once at the route" vs the deep snapshot path.** The route captures the flag for its own serialization, but the Gate #1 snapshot (`_idea_json`) is produced deep in `build_considered_set`/`confirm_gate1`, not at the route. This plan reads the same env flag via `feature_context_enabled()` inside `_idea_json` (env is immutable within a request, so the value is identical to what the route captured), matching the existing `gate1._scoped_applicability_enabled()` idiom rather than threading a boolean through `build_considered_set`. If the reviewer requires a literally-threaded boolean, Task 4 would instead add a `feature_context: bool` parameter down that call chain — a larger, cross-plan surgery flagged here.
3. **3A-ii `_idea_json` exact edit site.** Task 4 assumes 3A-ii added `validation_status`/`requirements` to `_idea_json` as trailing statements or literal keys; the step instructs verifying by symbol and, if they were added inside the dict literal, moving them into the flag-gated block. The resulting function is given in full to remove ambiguity.
4. **Cost/token keys in live `cost_metadata`.** `token_total` reads `input_tokens`/`output_tokens` defensively (absent → 0). If the live Claude client records token usage under different keys, the cost-regression bar could read 0 tokens and the `≤25%` assertion would be vacuously satisfied; the metric is written to be extended if the real cost_metadata key names differ. Flagged so the reviewer confirms the live key names when a key is available.
