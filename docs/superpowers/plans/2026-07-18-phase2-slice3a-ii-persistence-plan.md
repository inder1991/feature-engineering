# Slice 3A-ii — Carry the Honest Validation State End-to-End + Persist — Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (- [ ]) syntax.

**Goal:** Carry a feature's honest tri-state (`validation_status` + typed `requirements`) unbroken from the Gate #1 considered-set snapshot, through `ContractDraft`, the MCV, and `confirm_contract`, into a new pair of columns on the persisted `contract` row — so a `NEEDS_EXTERNAL_VALIDATION` feature is recorded honestly, never silently `DESIGN_CHECKED`.

**Architecture:** The tri-state already lives on `FeatureIdea` (added by 3A-i). This slice plumbs it through the four seams that currently drop it — the snapshot round-trip (`_idea_json`/`_idea_from_json`), `ContractDraft`, `validate_minimum` (MCV), and `confirm_contract` — and adds a migration giving `contract` its own `validation_status text` (CHECK'd) + `requirements jsonb` columns. `validation_status` is a **new axis**, distinct from the hyphenated `verification` stamp (which has its own CHECK'd vocabulary in migration 0973 and stays untouched).

## Global Constraints

- **Branch base:** the **3A-i branch tip** (this plan assumes 3A-i has already landed: `FeatureIdea` carries `operation_kind/measure_refs/grain_ref/time_ref/window/grouping_refs/validation_status/requirements`; `Requirement`, `REQUIREMENT_CODES`, `VALIDATION_STATES` exist in `feature_assist.py`; `_validate_idea` has the tri-state signature and returns a `FeatureIdea` carrying `validation_status` + `requirements`). Do **not** re-introduce those — consume them.
- **Implementers on FABLE, reviews on OPUS** (set the model explicitly per agent).
- **Shared-interface names (verbatim, do not redefine or drift):**
  - `VALIDATION_STATES = ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION", "REJECTED")`
  - `REQUIREMENT_CODES` (closed frozenset) — the vocabulary of `Requirement.code`.
  - `Requirement(code: str, operand: tuple[str, str], detail: str = "")` — `@dataclass(frozen=True, slots=True)`, from `feature_assist.py`.
  - `FeatureIdea.validation_status: str` (in `VALIDATION_STATES`) + `FeatureIdea.requirements: tuple[Requirement, ...]`.
  - `_validate_idea(...) -> tuple[FeatureIdea | None, Rejection | None]` — the returned idea carries `validation_status` + `requirements`; callers check `idea.validation_status`.
  - `ContractDraft` gains `validation_status: str` + `requirements: tuple[Requirement, ...]`.
- **Do NOT reuse** `governance/attributes.py` / `governance/predicates.py` `VERIFICATION_STAMPS` (a different table/vocabulary/axis).
- **Run pytest DIRECTLY** with `.venv/bin/python -m pytest …` — **never** pipe through `| tail` (or any pager/filter).
- **ruff line-length 100** — every task ends green under `.venv/bin/ruff check <files>` (config in `pyproject.toml`).
- **No placeholders** anywhere — no `...`, no TODO, no stub. Every test has concrete, real assertions; every implementation step shows the actual code.
- **Verify by symbol, not line number** — 3A-i shifted line numbers; open the real file and anchor on the dataclass/function name before editing.
- End git commit messages with:
  `Co-Authored-By: Claude <noreply@anthropic.com>`

---

## Task 1 — Migration: `contract.validation_status` + `contract.requirements`

**Files:**
- create `src/featuregen/db/migrations/1002_contract_validation_status.sql`
- test `tests/featuregen/overlay/upload/contract/test_validation_persistence.py` (new file)

**Interfaces:**
- **Consumes:** `VALIDATION_STATES = ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION", "REJECTED")` (the CHECK vocabulary).
- **Produces:** two new columns on the `contract` table — `validation_status text NOT NULL DEFAULT 'DESIGN_CHECKED'` (CHECK in `VALIDATION_STATES`) + `requirements jsonb NOT NULL DEFAULT '[]'::jsonb`. `1002` is the next free number (base has up to `1001_dispatch_flag_provenance.sql`).

Steps:

- [ ] Write the failing test. Create `tests/featuregen/overlay/upload/contract/test_validation_persistence.py` with:
  ```python
  """Slice 3A-ii — the honest validation state carried end-to-end and persisted on the contract row."""
  import psycopg
  import pytest

  from featuregen.intake.llm import FakeLLM, FakeResponse
  from featuregen.overlay.upload.canonical import CanonicalRow
  from featuregen.overlay.upload.contract.author import ContractDraft, draft_contract
  from featuregen.overlay.upload.contract.gate1 import ConsideredSet, _snapshot, chosen_feature
  from featuregen.overlay.upload.contract.govern import confirm_contract
  from featuregen.overlay.upload.contract.review import MinimumCheck, validate_minimum
  from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet, Requirement
  from featuregen.overlay.upload.graph import build_graph


  def _bank(db):
      build_graph(db, "bank", [
          CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
          CanonicalRow("bank", "accounts", "balance", "numeric",
                       definition="end-of-day ledger balance"),
          CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)])


  def _nev_idea():
      """A NEEDS_EXTERNAL_VALIDATION feature carrying one typed requirement on its measure column."""
      return FeatureIdea(
          name="avg_balance_90d", description="", derives_from=["public.accounts.balance"],
          aggregation="avg_90d", grain_table="accounts",
          derives_pairs=(("bank", "public.accounts.balance"),),
          validation_status="NEEDS_EXTERNAL_VALIDATION",
          requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                                    "declared numeric; operational type unknown"),))


  def test_contract_has_validation_status_and_requirements_columns(db):
      cols = dict(db.execute(
          "SELECT column_name, data_type FROM information_schema.columns "
          "WHERE table_name = 'contract' "
          "AND column_name IN ('validation_status', 'requirements')").fetchall())
      assert cols.get("validation_status") == "text"
      assert cols.get("requirements") == "jsonb"


  def test_contract_validation_status_check_rejects_unknown_value(db):
      with pytest.raises(psycopg.errors.CheckViolation):
          db.execute(
              "INSERT INTO contract "
              "(contract_id, feature_id, feature_name, version, validation_status) "
              "VALUES ('c-bogus', 'f-bogus', 'fx', 1, 'BOGUS')")


  def test_contract_validation_status_defaults_to_design_checked(db):
      db.execute(
          "INSERT INTO contract (contract_id, feature_id, feature_name, version) "
          "VALUES ('c-default', 'f-default', 'fd', 1)")
      row = db.execute(
          "SELECT validation_status, requirements FROM contract WHERE contract_id = 'c-default'"
      ).fetchone()
      assert row[0] == "DESIGN_CHECKED"
      assert row[1] == []
  ```
- [ ] Run it — expect **FAIL** (columns do not exist yet; the migration is absent):
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_contract_has_validation_status_and_requirements_columns tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_contract_validation_status_check_rejects_unknown_value tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_contract_validation_status_defaults_to_design_checked -x -q`
  (The import of `MinimumCheck` / `draft_contract` may also fail here — that is expected; later tasks add them. Run only these three node ids; they exercise the table shape once the migration lands.)
- [ ] Implement. Create `src/featuregen/db/migrations/1002_contract_validation_status.sql`:
  ```sql
  -- src/featuregen/db/migrations/1002_contract_validation_status.sql
  -- Phase-2 Slice 3 (3A-ii): carry the honest tri-state validation onto the persisted contract. A
  -- feature confirmed while NEEDS_EXTERNAL_VALIDATION must persist that HONESTLY, never be silently
  -- recorded as DESIGN_CHECKED. This is a NEW axis, SEPARATE from the hyphenated `verification` stamp
  -- (0968/0973): validation_status uses the underscore VALIDATION_STATES vocabulary and carries the
  -- typed requirements (each {code, operand:[catalog, object_ref], detail}). Re-confirm = a new row, so
  -- the history of what still needed external validation is preserved.
  ALTER TABLE contract ADD COLUMN IF NOT EXISTS validation_status text NOT NULL DEFAULT 'DESIGN_CHECKED';
  ALTER TABLE contract ADD COLUMN IF NOT EXISTS requirements       jsonb NOT NULL DEFAULT '[]'::jsonb;
  ALTER TABLE contract ADD CONSTRAINT contract_validation_status_ck
      CHECK (validation_status IN ('DESIGN_CHECKED', 'NEEDS_EXTERNAL_VALIDATION', 'REJECTED'));
  ```
- [ ] Run it — expect **PASS** (a fresh pytest session applies `1002` at setup):
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_contract_has_validation_status_and_requirements_columns tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_contract_validation_status_check_rejects_unknown_value tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_contract_validation_status_defaults_to_design_checked -x -q`
- [ ] `.venv/bin/ruff check src/featuregen/db/migrations tests/featuregen/overlay/upload/contract/test_validation_persistence.py` (SQL is not linted; this confirms the new test file is clean).
- [ ] Commit: `feat(contract): migration 1002 — contract.validation_status (CHECK) + requirements jsonb`

---

## Task 2 — `ContractDraft` gains `validation_status` + `requirements`; `draft_contract` populates them

**Files:**
- modify `src/featuregen/overlay/upload/contract/author.py`
- test `tests/featuregen/overlay/upload/contract/test_validation_persistence.py` (add a test)

**Interfaces:**
- **Consumes:** `FeatureIdea.validation_status: str`, `FeatureIdea.requirements: tuple[Requirement, ...]` (from 3A-i); `Requirement` (from `feature_assist.py`).
- **Produces:** `ContractDraft.validation_status: str = "DESIGN_CHECKED"`, `ContractDraft.requirements: tuple[Requirement, ...] = ()` — added **after** `join_path` with defaults so every existing positional/keyword construction site (`author.py:draft_contract`, `api/routes/contract.py:DraftIn.to_draft`, `test_govern._draft`) keeps working. `draft_contract` copies them from the chosen `FeatureIdea`.

Steps:

- [ ] Write the failing test. Append to `test_validation_persistence.py`:
  ```python
  def test_draft_contract_carries_validation_status_and_requirements(db):
      _bank(db)
      client = FakeLLM(script={"overlay.contract.draft": FakeResponse(
          output={"definition": "Average 90-day ledger balance per account."})})
      draft = draft_contract(db, _nev_idea(), client)
      assert draft.validation_status == "NEEDS_EXTERNAL_VALIDATION"
      assert draft.requirements == (
          Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                      "declared numeric; operational type unknown"),)


  def test_draft_defaults_are_design_checked_and_empty(db):
      _bank(db)
      client = FakeLLM(script={"overlay.contract.draft": FakeResponse(output={"definition": "x"})})
      plain = FeatureIdea(name="f", description="", derives_from=["public.accounts.balance"],
                          aggregation="avg_90d", grain_table="accounts",
                          derives_pairs=(("bank", "public.accounts.balance"),))
      draft = draft_contract(db, plain, client)
      assert draft.validation_status == "DESIGN_CHECKED"
      assert draft.requirements == ()
  ```
- [ ] Run it — expect **FAIL** (`ContractDraft` has no such fields; `draft_contract` does not set them):
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_draft_contract_carries_validation_status_and_requirements tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_draft_defaults_are_design_checked_and_empty -x -q`
- [ ] Implement. In `author.py`, extend the `feature_assist` import to include `Requirement`:
  ```python
  from featuregen.overlay.upload.feature_assist import FeatureIdea, Requirement
  ```
  Add the two fields at the END of the `ContractDraft` dataclass (after `join_path: tuple[dict, ...] = ()`):
  ```python
      # 3A-ii: the honest tri-state carried from the chosen FeatureIdea, so a NEEDS_EXTERNAL_VALIDATION
      # feature reaches confirm/persistence honestly (never silently DESIGN_CHECKED). This is a SEPARATE
      # axis from the hyphenated `verification` stamp; underscore VALIDATION_STATES vocabulary.
      validation_status: str = "DESIGN_CHECKED"
      requirements: tuple[Requirement, ...] = ()
  ```
  In `draft_contract`, extend the returned `ContractDraft(...)` — its last argument is currently
  `join_path=_join_path(conn, feature.grain_table, feature.derives_pairs, roles))`. Add the two fields:
  ```python
          join_path=_join_path(conn, feature.grain_table, feature.derives_pairs, roles),
          validation_status=feature.validation_status, requirements=feature.requirements)
  ```
- [ ] Run it — expect **PASS**:
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_draft_contract_carries_validation_status_and_requirements tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_draft_defaults_are_design_checked_and_empty -x -q`
- [ ] Regression — the existing author suite still passes (new fields defaulted):
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_author.py -q`
- [ ] `.venv/bin/ruff check src/featuregen/overlay/upload/contract/author.py tests/featuregen/overlay/upload/contract/test_validation_persistence.py`
- [ ] Commit: `feat(contract): ContractDraft carries validation_status + requirements from the chosen idea`

---

## Task 3 — Snapshot round-trip: serialize + RESTORE the honest state (`_idea_json` / `_idea_from_json`)

**Files:**
- modify `src/featuregen/overlay/upload/contract/_serial.py` (add the `Requirement` ⇄ JSON codec — the shared serialization home)
- modify `src/featuregen/overlay/upload/contract/gate1.py`
- test `tests/featuregen/overlay/upload/contract/test_validation_persistence.py` (add a test)

**Interfaces:**
- **Consumes:** `Requirement` (from `feature_assist.py`); `FeatureIdea.validation_status`/`.requirements`/`.verification`/`.critic_note`/`.rationale`.
- **Produces:**
  - `requirements_to_json(reqs: tuple[Requirement, ...]) -> list[dict]` and `requirements_from_json(data) -> tuple[Requirement, ...]` in `_serial.py` (each dict `{"code", "operand": [catalog, object_ref], "detail"}`).
  - `_idea_json` gains `validation_status` + `requirements` keys (it already serializes `verification`/`critic_note`/`rationale`).
  - `_idea_from_json` RESTORES `verification`, `critic_note`, `rationale` (currently dropped) **and** `validation_status` + `requirements`.

Steps:

- [ ] Write the failing test. Append to `test_validation_persistence.py`:
  ```python
  def test_snapshot_round_trips_validation_status_and_requirements(db):
      _bank(db)
      cs = ConsideredSet("intent-rt", None, [FeatureSet("templates", [_nev_idea()])], None)
      db.execute(
          "INSERT INTO contract_considered (intent_id, considered) VALUES (%s, %s::jsonb)",
          ("intent-rt", __import__("json").dumps(_snapshot(db, cs))))
      feat = chosen_feature(db, "intent-rt", "alternative", "avg_balance_90d")
      assert feat is not None
      assert feat.validation_status == "NEEDS_EXTERNAL_VALIDATION"
      assert feat.requirements == (
          Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                      "declared numeric; operational type unknown"),)


  def test_snapshot_restores_previously_dropped_verification_fields(db):
      _bank(db)
      idea = FeatureIdea(
          name="f", description="", derives_from=["public.accounts.balance"],
          aggregation="avg_90d", grain_table="accounts",
          derives_pairs=(("bank", "public.accounts.balance"),),
          verification="DESIGN-CHECKED", critic_note="weak grain fit", rationale="proxy for churn")
      cs = ConsideredSet("intent-vf", None, [FeatureSet("templates", [idea])], None)
      db.execute(
          "INSERT INTO contract_considered (intent_id, considered) VALUES (%s, %s::jsonb)",
          ("intent-vf", __import__("json").dumps(_snapshot(db, cs))))
      feat = chosen_feature(db, "intent-vf", "alternative", "f")
      assert feat is not None
      assert feat.verification == "DESIGN-CHECKED"
      assert feat.critic_note == "weak grain fit"      # was silently dropped pre-3A-ii
      assert feat.rationale == "proxy for churn"       # was silently dropped pre-3A-ii
      assert feat.validation_status == "DESIGN_CHECKED"
      assert feat.requirements == ()
  ```
- [ ] Run it — expect **FAIL** (`_idea_from_json` does not restore these fields):
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_snapshot_round_trips_validation_status_and_requirements tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_snapshot_restores_previously_dropped_verification_fields -x -q`
- [ ] Implement — the codec. In `_serial.py`, add the import and two functions (no circular import: `feature_assist` does not import the `contract` package):
  ```python
  from featuregen.overlay.upload.feature_assist import Requirement


  def requirements_to_json(reqs: tuple[Requirement, ...]) -> list[dict]:
      """Serialize typed requirements for a jsonb column / snapshot — {code, operand:[catalog, ref],
      detail}. Never carries a raw sample/PII value (detail is human-readable prose only)."""
      return [{"code": r.code, "operand": [r.operand[0], r.operand[1]], "detail": r.detail}
              for r in reqs]


  def requirements_from_json(data) -> tuple[Requirement, ...]:
      """Restore typed requirements from a jsonb column / snapshot. Tolerates a missing/None payload
      (-> empty tuple) so a pre-3A-ii snapshot deserializes as no requirements."""
      out: list[Requirement] = []
      for d in data or []:
          op = d.get("operand", ["", ""])
          out.append(Requirement(code=str(d.get("code", "")),
                                 operand=(str(op[0]), str(op[1])),
                                 detail=str(d.get("detail", ""))))
      return tuple(out)
  ```
- [ ] Implement — `gate1.py`. Add the codec import near the existing `_serial` import (`from featuregen.overlay.upload.contract._serial import actor_json as _actor_json`):
  ```python
  from featuregen.overlay.upload.contract._serial import (
      requirements_from_json,
      requirements_to_json,
  )
  ```
  Extend `_idea_json` — add two keys to the returned dict (keep the existing `verification`/`critic_note`/`rationale`/`derives_pairs` keys):
  ```python
              "validation_status": f.validation_status,   # 3A-ii honest tri-state (NEW axis)
              "requirements": requirements_to_json(f.requirements),
  ```
  Replace `_idea_from_json` wholesale so it restores every carried field (anchor on the `def _idea_from_json(d: dict) -> FeatureIdea:` symbol):
  ```python
  def _idea_from_json(d: dict) -> FeatureIdea:
      return FeatureIdea(
          name=d["name"], description="", derives_from=list(d.get("derives_from", [])),
          aggregation=d.get("aggregation"), grain_table=d.get("grain_table"),
          derives_pairs=tuple(tuple(p) for p in d.get("derives_pairs", [])),
          verification=d.get("verification", "DESIGN-CHECKED"),      # was dropped pre-3A-ii
          critic_note=d.get("critic_note", ""),                       # was dropped pre-3A-ii
          rationale=d.get("rationale", ""),                           # was dropped pre-3A-ii
          validation_status=d.get("validation_status", "DESIGN_CHECKED"),   # 3A-ii honest state
          requirements=requirements_from_json(d.get("requirements", [])))
  ```
- [ ] Run it — expect **PASS**:
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_snapshot_round_trips_validation_status_and_requirements tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_snapshot_restores_previously_dropped_verification_fields -x -q`
- [ ] Regression — the existing gate1 snapshot/round-trip suite still passes (additive keys, no exact-dict assertion):
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_gate1.py -q`
- [ ] `.venv/bin/ruff check src/featuregen/overlay/upload/contract/_serial.py src/featuregen/overlay/upload/contract/gate1.py tests/featuregen/overlay/upload/contract/test_validation_persistence.py`
- [ ] Commit: `fix(contract): snapshot round-trips validation_status + requirements (+ restore verification/critic_note/rationale)`

---

## Task 4 — MCV carries the tri-state forward: `validate_minimum` → `MinimumCheck`

**Files:**
- modify `src/featuregen/overlay/upload/contract/review.py`
- test `tests/featuregen/overlay/upload/contract/test_validation_persistence.py` (add tests)

**Interfaces:**
- **Consumes:** `_validate_idea(...) -> tuple[FeatureIdea | None, Rejection | None]` (3A-i signature; the returned idea carries `.validation_status` + `.requirements`); `Requirement`; `VALIDATION_STATES`.
- **Produces:** a new dataclass `MinimumCheck(ok: bool, reasons: list[str], validation_status: str, requirements: tuple[Requirement, ...])`, returned by `validate_minimum` **instead of** `tuple[bool, list[str]]`. `ok` is the govern gate; `validation_status`/`requirements` are the re-derived honest state; `reasons` is non-empty only when REJECTED. Callers (`author_contract` here, `confirm_contract` in Task 5) read the named fields.

> **Implementer note (cross-plan seam):** 3A-i already reshaped how `validate_minimum` builds the arguments it passes to `_validate_idea`. Do **not** rewrite that producing code. `validate_minimum` already binds the returned idea via `idea, reason = _validate_idea(...)`; 3A-ii changes **only** the return statement + the return-type annotation (and adds the `MinimumCheck` dataclass + import). The RED test below monkeypatches `_validate_idea`, so it is robust to whatever producing code the base has.

Steps:

- [ ] Write the failing tests. Append to `test_validation_persistence.py`:
  ```python
  def test_validate_minimum_carries_needs_external_validation(db, monkeypatch):
      _bank(db)
      draft = ContractDraft(
          "avg_balance_90d", "Average 90-day balance.", "accounts", "avg_90d", "posted_at",
          ["public.accounts.balance"], derives_pairs=(("bank", "public.accounts.balance"),),
          validation_status="NEEDS_EXTERNAL_VALIDATION",
          requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"), "x"),))
      crafted = FeatureIdea(
          name="avg_balance_90d", description="", derives_from=["public.accounts.balance"],
          aggregation="avg_90d", grain_table="accounts",
          derives_pairs=(("bank", "public.accounts.balance"),),
          validation_status="NEEDS_EXTERNAL_VALIDATION",
          requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"), "x"),))
      monkeypatch.setattr(
          "featuregen.overlay.upload.contract.review._validate_idea",
          lambda *a, **k: (crafted, None))
      check = validate_minimum(db, draft)
      assert isinstance(check, MinimumCheck)
      assert check.ok is True
      assert check.reasons == []
      assert check.validation_status == "NEEDS_EXTERNAL_VALIDATION"
      assert check.requirements == (
          Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"), "x"),)


  def test_validate_minimum_rejected_reports_reason_and_status(db):
      _bank(db)
      # derives from a column that does not exist -> the gauntlet REJECTS (ungrounded)
      draft = ContractDraft(
          "bad", "d", "accounts", "avg_90d", "posted_at", ["public.accounts.nope"],
          derives_pairs=(("bank", "public.accounts.nope"),))
      check = validate_minimum(db, draft)
      assert check.ok is False
      assert check.reasons                                 # a non-empty rejection reason
      assert check.validation_status == "REJECTED"
      assert check.requirements == ()


  def test_author_contract_consumes_minimumcheck(db):
      _bank(db)
      draft = ContractDraft(
          "avg_balance_90d", "Average 90-day balance.", "accounts", "avg_90d", "posted_at",
          ["public.accounts.balance"], derives_pairs=(("bank", "public.accounts.balance"),))
      client = FakeLLM(script={
          "overlay.contract.critique": FakeResponse(output={"findings": []}),
          "overlay.contract.refine": FakeResponse(output={"definition": "Average 90-day balance."})})
      from featuregen.overlay.upload.contract.review import author_contract
      result_draft, unresolved = author_contract(db, draft, client)
      assert unresolved == []                              # MCV clean + no critique -> nothing unresolved
      assert result_draft.feature_name == "avg_balance_90d"
  ```
- [ ] Run it — expect **FAIL** (`MinimumCheck` does not exist; `validate_minimum` returns a tuple):
  `.venv/bin/python -m pytest "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_validate_minimum_carries_needs_external_validation" "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_validate_minimum_rejected_reports_reason_and_status" "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_author_contract_consumes_minimumcheck" -x -q`
- [ ] Implement — `review.py`. Extend the imports:
  ```python
  from dataclasses import dataclass, replace
  ```
  ```python
  from featuregen.overlay.upload.feature_assist import Requirement, _validate_idea
  ```
  Add the `MinimumCheck` dataclass just above `validate_minimum`:
  ```python
  @dataclass(frozen=True, slots=True)
  class MinimumCheck:
      """MCV outcome carrying the tri-state forward (3A-ii). `ok` is the govern gate (a REJECTED draft
      must never be governed); `validation_status` + `requirements` are the honest state re-derived from
      the LIVE catalog; `reasons` is non-empty only when REJECTED. Replaces the old (bool, list[str])."""
      ok: bool
      reasons: list[str]
      validation_status: str
      requirements: tuple[Requirement, ...]
  ```
  Change `validate_minimum`'s return annotation from `-> tuple[bool, list[str]]` to `-> MinimumCheck`. Keep the body that builds `raw`/`known`/`src_of` and the `idea, reason = _validate_idea(...)` call **exactly as the 3A-i base has it**; replace only the final `return (...)` line with:
  ```python
      if idea is None:
          return MinimumCheck(ok=False, reasons=[reason.message],
                              validation_status="REJECTED", requirements=())
      return MinimumCheck(ok=True, reasons=[], validation_status=idea.validation_status,
                          requirements=idea.requirements)
  ```
  Update `author_contract`'s three uses of `validate_minimum` (anchor on `def author_contract`). Its loop currently unpacks `_, mcv = validate_minimum(...)`; rewrite to read `.reasons`:
  ```python
      for _ in range(budget):
          check = validate_minimum(conn, draft, target_ref=tref, now=now)
          critique = critique_contract(conn, draft, client, actor=actor)
          if not check.reasons and not critique:
              return draft, []                       # clean
          if check.reasons and not critique:
              return draft, check.reasons            # structural defect the LLM can't fix → surface
          draft = refine_contract(conn, draft, check.reasons + critique, client, actor=actor)
      check = validate_minimum(conn, draft, target_ref=tref, now=now)
      return draft, check.reasons
  ```
- [ ] Run it — expect **PASS**:
  `.venv/bin/python -m pytest "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_validate_minimum_carries_needs_external_validation" "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_validate_minimum_rejected_reports_reason_and_status" "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_author_contract_consumes_minimumcheck" -x -q`
- [ ] Regression — the existing review suite still passes:
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_review.py -q`
- [ ] `.venv/bin/ruff check src/featuregen/overlay/upload/contract/review.py tests/featuregen/overlay/upload/contract/test_validation_persistence.py`
- [ ] Commit: `refactor(contract): validate_minimum returns MinimumCheck carrying status + requirements`

---

## Task 5 — `confirm_contract` persists `draft.validation_status` + `draft.requirements`

**Files:**
- modify `src/featuregen/overlay/upload/contract/govern.py`
- test `tests/featuregen/overlay/upload/contract/test_validation_persistence.py` (add a test)

**Interfaces:**
- **Consumes:** `MinimumCheck` (Task 4) via `validate_minimum`; `ContractDraft.validation_status`/`.requirements` (Task 2); `requirements_to_json` (Task 3).
- **Produces:** the `INSERT INTO contract` writes the new `validation_status` + `requirements` columns from the draft. The govern gate reads `check.ok` (was the old `ok` bool).

> **Reconciliation of spec §3.4 with the DB + the invariant:** §3.4 says "write `draft.validation_status` at the three hardcoded `DESIGN-CHECKED` sites." Those three sites write the **hyphenated `verification` stamp**, whose CHECK constraints (`feature_verification_ck` / `contract_verification_ck`, migration 0973) admit **only** `UNVERIFIED/DESIGN-CHECKED/DATA-CHECKED/USEFULNESS-CHECKED`. Writing an underscore `validation_status` there would violate the CHECK **and** the cross-cutting invariant that `validation_status` is a **new axis, not a repurposing of `verification`**. So 3A-ii records the honest state in the **new `contract.validation_status`/`requirements` columns** (the axis the e2e asserts), and leaves the three hyphenated `verification` writes unchanged. `confirm_contract` persists **`draft.validation_status`** (the state carried from Gate #1), **not** the MCV re-run's fresh status — a clean re-run must never silently promote a `NEEDS_EXTERNAL_VALIDATION` feature to `DESIGN_CHECKED`.

Steps:

- [ ] Write the failing test. Append to `test_validation_persistence.py`:
  ```python
  def test_confirm_persists_validation_status_and_requirements(db):
      _bank(db)
      draft = ContractDraft(
          "avg_balance_90d", "Average 90-day ledger balance per account.", "accounts", "avg_90d",
          "posted_at", ["public.accounts.balance"],
          derives_pairs=(("bank", "public.accounts.balance"),),
          validation_status="NEEDS_EXTERNAL_VALIDATION",
          requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                                    "declared numeric; operational type unknown"),))
      c = confirm_contract(db, draft, actor="ds1")
      row = db.execute(
          "SELECT validation_status, requirements, verification FROM contract "
          "WHERE contract_id = %s", (c.contract_id,)).fetchone()
      assert row[0] == "NEEDS_EXTERNAL_VALIDATION"                # honest, not silently DESIGN_CHECKED
      assert row[1] == [{"code": "TYPE_IS_NUMERIC",
                         "operand": ["bank", "public.accounts.balance"],
                         "detail": "declared numeric; operational type unknown"}]
      assert row[2] == "DESIGN-CHECKED"                           # the SEPARATE verification axis intact


  def test_confirm_default_draft_persists_design_checked(db):
      _bank(db)
      draft = ContractDraft(
          "avg_balance_90d", "Average 90-day ledger balance.", "accounts", "avg_90d", "posted_at",
          ["public.accounts.balance"], derives_pairs=(("bank", "public.accounts.balance"),))
      c = confirm_contract(db, draft, actor="ds1")
      row = db.execute("SELECT validation_status, requirements FROM contract WHERE contract_id = %s",
                       (c.contract_id,)).fetchone()
      assert row[0] == "DESIGN_CHECKED"
      assert row[1] == []
  ```
- [ ] Run it — expect **FAIL** (`confirm_contract` neither writes nor persists the new columns; they default):
  `.venv/bin/python -m pytest "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_confirm_persists_validation_status_and_requirements" "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_confirm_default_draft_persists_design_checked" -x -q`
- [ ] Implement — `govern.py`. Extend the `_serial` import (currently `from featuregen.overlay.upload.contract._serial import actor_json as _actor_json`) to also import the codec:
  ```python
  from featuregen.overlay.upload.contract._serial import actor_json as _actor_json
  from featuregen.overlay.upload.contract._serial import requirements_to_json
  ```
  In `confirm_contract`, change the MCV gate to read the `MinimumCheck` (anchor on `ok, reasons = validate_minimum(`):
  ```python
      check = validate_minimum(conn, draft, target_ref=tref, now=now)
      if not check.ok:
          raise ContractValidationError(f"contract failed MCV, not governed: {check.reasons}")
  ```
  Extend the `INSERT INTO contract` (anchor on `"INSERT INTO contract (contract_id, feature_id,`) to write the two new columns from the draft; the hyphenated `verification` value stays `"DESIGN-CHECKED"`:
  ```python
      conn.execute(
          "INSERT INTO contract (contract_id, feature_id, feature_name, definition, version, actor, "
          "join_path, intent_id, verification, validation_status, requirements) "
          "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb)",
          (contract_id, feature_id, draft.feature_name, draft.definition, version, _actor_json(actor),
           json.dumps(list(draft.join_path)), intent_id,   # intent_id: audit link to the hypothesis (M5)
           "DESIGN-CHECKED",                                # §14.5 verification stamp — SEPARATE axis
           draft.validation_status,                         # 3A-ii: the honest tri-state, carried
           json.dumps(requirements_to_json(draft.requirements))))
      return Contract(contract_id, feature_id, draft.feature_name, version)
  ```
  (Leave the `UPDATE feature ... verification = %s` and `register_feature(..., verification="DESIGN-CHECKED")` writes untouched — the verification axis is unchanged.)
- [ ] Run it — expect **PASS**:
  `.venv/bin/python -m pytest "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_confirm_persists_validation_status_and_requirements" "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_confirm_default_draft_persists_design_checked" -x -q`
- [ ] Regression — the existing govern suite still passes (verification axis + versioning unchanged):
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_govern.py -q`
- [ ] `.venv/bin/ruff check src/featuregen/overlay/upload/contract/govern.py tests/featuregen/overlay/upload/contract/test_validation_persistence.py`
- [ ] Commit: `feat(contract): confirm_contract persists validation_status + requirements (verification axis untouched)`

---

## Task 6 — End-to-end: `NEEDS_EXTERNAL_VALIDATION` survives snapshot → draft → MCV → confirm

**Files:**
- test `tests/featuregen/overlay/upload/contract/test_validation_persistence.py` (add the e2e test — no source change; this proves the seams from Tasks 1–5 compose)

**Interfaces:**
- **Consumes, in order:** `_snapshot`/`chosen_feature` (Task 3) → `draft_contract`/`ContractDraft` (Task 2) → `validate_minimum`/`MinimumCheck` (Task 4) → `confirm_contract` (Task 5) → the persisted `contract.validation_status`/`requirements` columns (Task 1). No `governance/attributes.py` `VERIFICATION_STAMPS`.

Steps:

- [ ] Write the failing test (it will pass once Tasks 1–5 are merged; if any seam regresses it goes red). Append to `test_validation_persistence.py`:
  ```python
  def test_needs_external_validation_survives_gate1_to_persisted_contract(db):
      """The full honest path: a NEEDS_EXTERNAL_VALIDATION feature chosen at Gate #1 is snapshotted,
      reconstructed, drafted, re-validated (MCV), and confirmed — and the CONTRACT ROW records
      NEEDS_EXTERNAL_VALIDATION + its requirement, NOT a silent DESIGN_CHECKED. The hyphenated
      verification stamp stays a SEPARATE axis (does NOT reuse governance VERIFICATION_STAMPS)."""
      _bank(db)
      # Gate #1: the human's chosen option lands in the persisted considered-set snapshot.
      cs = ConsideredSet("intent-e2e", None, [FeatureSet("templates", [_nev_idea()])], None)
      db.execute(
          "INSERT INTO contract_considered (intent_id, considered) VALUES (%s, %s::jsonb)",
          ("intent-e2e", __import__("json").dumps(_snapshot(db, cs))))
      # Reconstruct the chosen feature from the SERVER snapshot (honest state must survive).
      chosen = chosen_feature(db, "intent-e2e", "alternative", "avg_balance_90d")
      assert chosen is not None and chosen.validation_status == "NEEDS_EXTERNAL_VALIDATION"
      # Author the draft; the state rides onto the draft.
      client = FakeLLM(script={"overlay.contract.draft": FakeResponse(
          output={"definition": "Average 90-day ledger balance per account."})})
      draft = draft_contract(db, chosen, client)
      assert draft.validation_status == "NEEDS_EXTERNAL_VALIDATION"
      # MCV re-runs and passes the gate (grounded, fresh — no `now` so freshness is skipped).
      check = validate_minimum(db, draft)
      assert check.ok is True
      # Confirm persists the CARRIED honest state (not the re-run's fresh classification).
      c = confirm_contract(db, draft, actor="ds1", intent_id="intent-e2e")
      row = db.execute(
          "SELECT validation_status, requirements, verification FROM contract "
          "WHERE contract_id = %s", (c.contract_id,)).fetchone()
      assert row[0] == "NEEDS_EXTERNAL_VALIDATION"
      assert row[1] == [{"code": "TYPE_IS_NUMERIC",
                         "operand": ["bank", "public.accounts.balance"],
                         "detail": "declared numeric; operational type unknown"}]
      assert row[2] == "DESIGN-CHECKED"      # verification is a distinct axis, not overwritten
  ```
- [ ] Run it — expect **PASS** (all seams present after Tasks 1–5):
  `.venv/bin/python -m pytest "tests/featuregen/overlay/upload/contract/test_validation_persistence.py::test_needs_external_validation_survives_gate1_to_persisted_contract" -x -q`
- [ ] Full-file green — run the whole new suite plus the four touched contract suites directly (no pager):
  `.venv/bin/python -m pytest tests/featuregen/overlay/upload/contract/test_validation_persistence.py tests/featuregen/overlay/upload/contract/test_author.py tests/featuregen/overlay/upload/contract/test_gate1.py tests/featuregen/overlay/upload/contract/test_review.py tests/featuregen/overlay/upload/contract/test_govern.py -q`
- [ ] `.venv/bin/ruff check tests/featuregen/overlay/upload/contract/test_validation_persistence.py`
- [ ] Commit: `test(contract): e2e — NEEDS_EXTERNAL_VALIDATION survives Gate #1 → draft → MCV → confirm`

---

## Self-Review

**Spec coverage (§3 of the design + the 3A-ii scope):**
- §3.5 migration — `contract.validation_status text` (+ CHECK in `VALIDATION_STATES`) and `contract.requirements jsonb`: **Task 1** (`1002_contract_validation_status.sql`, next free number; base had up to `1001`).
- §3.2 `ContractDraft` gains `validation_status` + `requirements`, populated in `draft_contract`: **Task 2**.
- §3.1 `_idea_json` extended (serialize `validation_status` + `requirements`) and `_idea_from_json` RESTORES them **and** the previously-dropped `verification`/`critic_note`/`rationale`: **Task 3** (codec in `_serial.py`).
- §3.3 `validate_minimum` carries requirements + status forward (not `tuple[bool, list[str]]`): **Task 4** (`MinimumCheck`; `author_contract` call sites updated).
- §3.4 `confirm_contract` records `draft.validation_status` (not a hardcoded stamp) — reconciled against the `verification` CHECK + the separate-axis invariant by writing the new columns, verification untouched: **Task 5**.
- E2e: `NEEDS_EXTERNAL_VALIDATION` survives Gate #1 snapshot → draft → MCV → confirm → the contract row shows `NEEDS_EXTERNAL_VALIDATION` + its requirements: **Task 6**.
- Invariant honored: `validation_status` is a NEW axis; `governance/attributes.py`/`predicates.py` `VERIFICATION_STAMPS` is **not** reused; the hyphenated `verification` stamp is unchanged.

**Placeholder scan:** no `...`, no `TODO`, no stub. Every test carries concrete assertions (exact strings, exact jsonb list-of-dict, `Requirement(...)` equality via the frozen dataclass). Every implementation step shows the actual code (migration SQL, dataclass fields, codec, edited SQL INSERT).

**Type consistency vs the shared contract:**
- `VALIDATION_STATES` values `("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION", "REJECTED")` used verbatim in the migration CHECK, defaults, and assertions (underscore vocabulary — distinct from the hyphenated `verification`).
- `Requirement(code, operand: tuple[str, str], detail="")` consumed unchanged; the jsonb shape `{"code", "operand": [catalog, object_ref], "detail"}` matches its fields.
- `REQUIREMENT_CODES` respected — the only requirement code used is `TYPE_IS_NUMERIC` (a member).
- `FeatureIdea.validation_status` / `.requirements` consumed as defined by 3A-i; new construction sites use keyword args so 3A-i's added fields never shift positionally.
- `ContractDraft.validation_status: str` + `.requirements: tuple[Requirement, ...]` added exactly as the shared contract's Persistence section prescribes; defaults keep existing construction sites (route `DraftIn.to_draft`, `test_govern._draft`) valid.
- `_validate_idea`'s producing call inside `validate_minimum` is left to the 3A-i base; only the packaging return changes — so this plan does not drift 3A-i's signature.

**New type introduced by this plan (not in the shared contract):** `MinimumCheck` (the MCV return). The shared contract mandates `validate_minimum` "carry the requirements + status forward, not a bare `tuple[bool, list[str]]`" but does not pin a type name; `MinimumCheck(ok, reasons, validation_status, requirements)` is that carrier. Flagged below for cross-plan awareness.

**Ambiguities / concerns (surfaced for the orchestrator):**
1. **§3.4 vs. the DB + invariant (resolved in-plan, worth confirming):** the three hardcoded `"DESIGN-CHECKED"` sites write the hyphenated `verification` column, which is CHECK-constrained (0973) to the hyphenated stamp vocabulary; the cross-cutting invariant forbids repurposing `verification`. A literal "write `draft.validation_status` at those three sites" is therefore impossible/invariant-breaking. This plan records the honest state in the **new** `contract.validation_status`/`requirements` columns and leaves `verification` a distinct axis. The e2e asserts exactly this.
2. **`MinimumCheck` is a plan-local type** — if 3A-iii/3A-iv also consume `validate_minimum`, they must import `MinimumCheck` from `contract/review.py` and read `.ok`/`.reasons`/`.validation_status`/`.requirements` (no tuple unpacking).
3. **Confirm persists `draft.validation_status`, not the MCV re-run's fresh status.** This is deliberate (the honest state is the one the human saw at Gate #1; a clean re-run must not silently promote to `DESIGN_CHECKED`). If the intended semantics were instead "persist the freshest re-run status," Task 5 + the e2e would need to flip to `check.validation_status`/`check.requirements` — confirm before build.
4. **Cross-plan base dependency:** this plan assumes 3A-i has landed the tri-state `FeatureIdea` fields, `Requirement`/`VALIDATION_STATES`/`REQUIREMENT_CODES`, and the reshaped `_validate_idea`. `validate_minimum`'s pre-call body is treated as base-owned (only the return is changed), which insulates 3A-ii from 3A-i's exact call form — but the implementer must open the real (post-3A-i) `review.py`/`feature_assist.py` before editing.
5. **Out-of-scope honesty gap (noted, not fixed here):** the direct HTTP draft path `api/routes/contract.py:DraftIn.to_draft` constructs `ContractDraft` from client fields and would default `validation_status` to `DESIGN_CHECKED` (the task scopes `draft_contract`, not the route). The server-reconstructed `chosen_feature` path is the honest one; the client-draft path is a pre-existing trust boundary. Flag for a follow-up (thread the state through `DraftIn`, or re-derive it server-side at draft time).
6. **Typed operands not snapshotted:** per the 3A-ii scope, `_idea_json`/`_idea_from_json` round-trip `validation_status` + `requirements` (which already carry the operand refs), not the raw typed operands (`measure_refs`/`grain_ref`/`time_ref`/`window`/`grouping_refs`). The MCV re-derives operands from `derives_pairs`, so this is safe for 3A-ii; full typed-operand round-trip is a 3A-i/follow-up concern.
7. **Snapshot bytes change** (new `validation_status`/`requirements` keys): expected and required for honesty. 3A-iv's flag-off byte-identity serializer (§8) is what later gates this behind `FEATUREGEN_FEATURE_CONTEXT`; not a 3A-ii concern.
