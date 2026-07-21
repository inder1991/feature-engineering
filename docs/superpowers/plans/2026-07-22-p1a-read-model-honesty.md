# P1a — Read-Model Honesty Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the asset-detail screen show a value's real author instead of "unattested", and collapse the Relationships tab's 125-row column dump to a one-line link.

**Architecture:** The read model already emits the evidence layer (`asset_detail._evidence_section`); the frontend just leads with the empty governed-decision layer. This plan (a) adds an `evidence_provenance` fallback to each `effective_metadata` field on the backend and (b) renders it on the frontend, then (c) demotes the Relationships containment dump. AI-independent, no authority-tier change, no async, no migration.

**Tech Stack:** Python 3.12 / psycopg / pytest (backend); React 18 / TypeScript / vitest (frontend).

## Global Constraints

- Scope is `src/featuregen/overlay/upload/asset_detail.py` + `frontend/src/screens/AssetDetailScreen.tsx` + `frontend/src/api.ts` + their tests. No migration, no authority-tier change, no async work.
- Backend tests: `.venv/bin/python -m pytest <path> -q`. ruff line-length 100 on touched files.
- Frontend tests: `cd frontend && npx vitest run <path> --pool=forks` (the default pool HANGS in this env — always pass `--pool=forks`). `npx tsc -b` and `npx oxlint <files>` clean.
- Stage ONLY the files you touch (the worktree carries unrelated dirt: deploy/kind, uv.lock, docs). Never `git add -A`.
- Do not change `read_operational_value`, `field_authority`, `field_policies`, or any resolution-core file — P1a is display-layer only.

## File Structure

- `src/featuregen/overlay/upload/asset_detail.py` — MODIFY `_effective_metadata_section` (add `evidence_provenance`); ADD two module helpers (`_EVIDENCE_PROVENANCE_LABELS`, `_evidence_provenance_label`).
- `frontend/src/api.ts` — MODIFY `EffectiveMetadataField` (add `evidence_provenance`).
- `frontend/src/screens/AssetDetailScreen.tsx` — ADD `attestedByLabel`; use it in `AuthorityBadge` + `MetadataTab`; MODIFY `RelationshipsTab` containment section.
- Tests: `tests/featuregen/overlay/upload/test_asset_detail_provenance.py` (new), `frontend/src/screens/AssetDetailScreen.test.tsx` (extend).

---

### Task 1: Backend — `evidence_provenance` fallback on each metadata field

**Files:**
- Modify: `src/featuregen/overlay/upload/asset_detail.py` (`_effective_metadata_section` ~139-166; add helpers near `_authority_label` ~129-137)
- Test: `tests/featuregen/overlay/upload/test_asset_detail_provenance.py` (create)

**Interfaces:**
- Consumes: `record_field_evidence(conn, *, logical_ref, field_name, proposed_value, producer, strength, producer_ref, source_snapshot_id, input_hash, lifecycle=...)` (field_evidence.py); `build_asset_detail(conn, *, source, object_ref, roles, identity, include=[...])`; `build_graph(conn, source, [CanonicalRow(...)])`.
- Produces: each `effective_metadata.fields[label]` dict gains `"evidence_provenance": str | None` — a human label of the newest ACTIVE `field_evidence` for that field, or `None`.

- [ ] **Step 1: Write the failing test**

Create `tests/featuregen/overlay/upload/test_asset_detail_provenance.py`:

```python
"""P1a — effective_metadata surfaces a value's evidence provenance when there is no governed decision,
so a known-author value never reads as 'unattested'."""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

ADMIN = mint_test_identity(subject="user:admin", role_claims=("platform_admin",))


def _concept_field(conn, source):
    body = build_asset_detail(conn, source=source, object_ref="public.trades.notional",
                              roles=list(ADMIN.role_claims), identity=ADMIN,
                              include=["effective_metadata"])
    return body["effective_metadata"]["fields"]["concept"]


def test_unconfirmed_value_carries_its_evidence_provenance(overlay_conn):
    source = "prov_ai"
    build_graph(overlay_conn, source, [CanonicalRow(source, "trades", "notional", "numeric")])
    record_field_evidence(
        overlay_conn, logical_ref=f"{source}::public.trades.notional", field_name="concept",
        proposed_value="monetary_flow", producer="llm", strength="proposed",
        producer_ref="test", source_snapshot_id="snap", input_hash="h1",
    )
    field = _concept_field(overlay_conn, source)
    assert field["provenance"] is None                     # no governed decision
    assert field["evidence_provenance"] == "AI proposed"    # but the author is known


def test_source_attested_value_reads_source_attested(overlay_conn):
    source = "prov_src"
    build_graph(overlay_conn, source, [CanonicalRow(source, "trades", "notional", "numeric")])
    record_field_evidence(
        overlay_conn, logical_ref=f"{source}::public.trades.notional", field_name="concept",
        proposed_value="monetary_flow", producer="source", strength="attested",
        producer_ref="test", source_snapshot_id="snap", input_hash="h2",
    )
    assert _concept_field(overlay_conn, source)["evidence_provenance"] == "source attested"


def test_no_evidence_leaves_provenance_none(overlay_conn):
    source = "prov_none"
    build_graph(overlay_conn, source, [CanonicalRow(source, "trades", "notional", "numeric")])
    field = _concept_field(overlay_conn, source)
    assert field["provenance"] is None
    assert field["evidence_provenance"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_asset_detail_provenance.py -q`
Expected: FAIL with `KeyError: 'evidence_provenance'`.

- [ ] **Step 3: Add the label helpers**

In `asset_detail.py`, immediately after `_authority_label` (~line 137), add:

```python
# Honest author of an UNCONFIRMED value: who asserted it and at what strength, from the active
# evidence layer. Surfaced as effective_metadata's fallback so a value with a known author never reads
# as "unattested" just because no governed DECISION exists yet.
_EVIDENCE_PROVENANCE_LABELS: dict[tuple[str, str], str] = {
    ("source", "attested"): "source attested",
    ("source", "proposed"): "source proposed",
    ("llm", "proposed"): "AI proposed",
    ("llm", "corroborated"): "AI corroborated",
    ("taxonomy", "confirmed"): "rulebook confirmed",
    ("taxonomy", "proposed"): "rulebook proposed",
    ("parser", "supported"): "parser detected",
}


def _evidence_provenance_label(producer: str, strength: str) -> str:
    return _EVIDENCE_PROVENANCE_LABELS.get((producer, strength), f"{producer} {strength}")
```

- [ ] **Step 4: Wire the fallback into `_effective_metadata_section`**

In `_effective_metadata_section`, after `fields: dict[str, dict] = {}` (~line 143), add the one-shot query:

```python
    # Newest ACTIVE evidence per field — the honest author of a value that has no governed decision.
    active_ev = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT DISTINCT ON (field_name) field_name, producer, strength "
            "FROM field_evidence WHERE logical_ref = %s AND lifecycle = 'active' "
            "ORDER BY field_name, created_at DESC, evidence_id DESC",
            (logical_ref,),
        ).fetchall()
    }
```

Then inside the `for label, flat_col, c1_field in _METADATA_FIELDS:` loop, add to the `entry` dict (after `"selected_evidence_ids": ...`):

```python
        ev = active_ev.get(c1_field)
        entry["evidence_provenance"] = (
            _evidence_provenance_label(ev[0], ev[1]) if ev else None
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_asset_detail_provenance.py -q`
Expected: PASS (3 passed). Then ruff: `.venv/bin/ruff check src/featuregen/overlay/upload/asset_detail.py` → clean.

- [ ] **Step 6: Guard the existing asset-detail suite**

Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/test_asset_detail_semantic.py tests/featuregen/overlay/upload/test_asset_detail_entity_authority.py tests/featuregen/api/test_assets.py -q`
Expected: PASS (no regressions — the new key is additive).

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/overlay/upload/asset_detail.py tests/featuregen/overlay/upload/test_asset_detail_provenance.py
git commit -m "feat(asset-detail): surface evidence_provenance fallback so known-author values are not 'unattested'"
```

---

### Task 2: Frontend — render the evidence-provenance fallback instead of "unattested"

**Files:**
- Modify: `frontend/src/api.ts` (`EffectiveMetadataField` ~1578)
- Modify: `frontend/src/screens/AssetDetailScreen.tsx` (add `attestedByLabel`; `AuthorityBadge` ~333-342; `MetadataTab` ~470)
- Test: `frontend/src/screens/AssetDetailScreen.test.tsx` (extend; existing badge test ~211-226)

**Interfaces:**
- Consumes: `EffectiveMetadataField.evidence_provenance: string | null` from Task 1.
- Produces: `attestedByLabel(field): string` — the label a field's badge shows.

- [ ] **Step 1: Write the failing test**

In `AssetDetailScreen.test.tsx`, add inside the top-level `describe` (after the existing badge test ~line 226):

```tsx
  it('falls back to the evidence author when there is no decision, only "unattested" when truly nothing', async () => {
    getAssetDetail.mockResolvedValue(assetDetail({
      effective_metadata: { fields: {
        concept: { value: 'monetary_flow', authority: 'hint', c1_status: 'no_decision',
                   provenance: null, evidence_provenance: 'AI proposed', selected_evidence_ids: [] },
        unit:    { value: null, authority: 'missing', c1_status: 'no_decision',
                   provenance: null, evidence_provenance: null, selected_evidence_ids: [] },
      } },
    }))
    render(<AssetDetailScreen source="ftr" objectRef="public.t.c" />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Metadata & evidence' }))
    expect(authorityChip('AI proposed', 'gj-proposed')).toBeTruthy()   // known author, not "unattested"
    expect(authorityChip('unattested', 'gj-none')).toBeTruthy()        // genuinely nothing
  })
```

(Use the file's existing `assetDetail(...)` factory and `authorityChip` helper; match the `EffectiveMetadataField` shape already used in the sibling badge test, adding `evidence_provenance`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/screens/AssetDetailScreen.test.tsx --pool=forks`
Expected: FAIL — the concept badge renders "unattested" (from `provenanceLabel(null)`), not "AI proposed".

- [ ] **Step 3: Add `evidence_provenance` to the type**

In `frontend/src/api.ts`, in `interface EffectiveMetadataField` (~1578), add after `provenance: string | null`:

```ts
  evidence_provenance: string | null
```

- [ ] **Step 4: Add `attestedByLabel` and use it**

In `AssetDetailScreen.tsx`, after `provenanceLabel` (~line 62), add:

```tsx
// The badge shows the value's author: the governed decision provenance if any, else the evidence-layer
// author (source attested / AI proposed / rulebook proposed), else "unattested" only when truly nothing.
function attestedByLabel(field: EffectiveMetadataField): string {
  if (field.provenance) return provenanceLabel(field.provenance)
  if (field.evidence_provenance) return field.evidence_provenance
  return 'unattested'
}
```

In `AuthorityBadge` (~line 339) replace `{provenanceLabel(field.provenance)}` with `{attestedByLabel(field)}`.

In `MetadataTab` (~line 470) replace `Attested by <strong>{provenanceLabel(field.provenance)}</strong>.` with `Attested by <strong>{attestedByLabel(field)}</strong>.`

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/screens/AssetDetailScreen.test.tsx --pool=forks`
Expected: PASS (including the pre-existing badge test — its "missing" field now needs `evidence_provenance: null` in its mock; update that mock if the pre-existing test fails on the new required field).

- [ ] **Step 6: Typecheck + lint**

Run: `cd frontend && npx tsc -b && npx oxlint src/screens/AssetDetailScreen.tsx src/api.ts`
Expected: clean. (Every `EffectiveMetadataField` mock in the test file now needs `evidence_provenance` — tsc will name any that are missing; add `evidence_provenance: null` to them.)

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api.ts frontend/src/screens/AssetDetailScreen.tsx frontend/src/screens/AssetDetailScreen.test.tsx
git commit -m "feat(asset-detail): render evidence author instead of 'unattested' for known-author values"
```

---

### Task 3: Frontend — demote the Relationships containment dump to a one-line link

**Files:**
- Modify: `frontend/src/screens/AssetDetailScreen.tsx` (`RelationshipsTab` containment section ~560-576)
- Test: `frontend/src/screens/AssetDetailScreen.test.tsx` (extend)

**Interfaces:**
- Consumes: `detail.relationships.containment.{table, columns}` (unchanged shape).
- Produces: no new interface; the containment section renders a summary line, not a per-column list.

- [ ] **Step 1: Write the failing test**

In `AssetDetailScreen.test.tsx`, add:

```tsx
  it('containment is a one-line summary, not a dump of every sibling column', async () => {
    getAssetDetail.mockResolvedValue(assetDetail({
      relationships: {
        containment: {
          table: { object_ref: 'public.comp_financial_tran_repos_dly', table: 'comp_financial_tran_repos_dly' },
          columns: [
            { object_ref: 'public.t.actual_tran_amt', column: 'actual_tran_amt', data_type: 'unknown', sensitivity: null },
            { object_ref: 'public.t.cif_id', column: 'cif_id', data_type: 'unknown', sensitivity: null },
          ],
        },
        approved_joins: [],
        semantic: { status: 'available', verified_edges: [], candidates: [], divergences: [] },
      },
    }))
    render(<AssetDetailScreen source="ftr" objectRef="public.t.cif_id" />)
    await userEvent.click(await screen.findByRole('tab', { name: 'Relationships' }))
    expect(await screen.findByText(/2 other columns/)).toBeInTheDocument()   // the summary line
    expect(screen.queryByText('actual_tran_amt')).not.toBeInTheDocument()    // NOT dumped as a row
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/screens/AssetDetailScreen.test.tsx --pool=forks`
Expected: FAIL — `actual_tran_amt` is still rendered as a row, and there is no "2 other columns" summary.

- [ ] **Step 3: Replace the containment section**

In `AssetDetailScreen.tsx`, replace the containment `<section>` (~lines 560-576, the `<h3>Containment</h3>` through the closing `</section>` that maps `rel.containment.columns`) with:

```tsx
      <section className="adg-section">
        <h3 className="micro-label">Containment</h3>
        <p className="hint">
          Belongs to <span className="mono">{rel.containment.table.object_ref}</span>
          {' · '}
          {rel.containment.columns.length}{' '}
          {rel.containment.columns.length === 1 ? 'other column' : 'other columns'}
        </p>
      </section>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/screens/AssetDetailScreen.test.tsx --pool=forks`
Expected: PASS. (If a pre-existing test asserted a specific sibling column renders in Containment, update it to assert the summary line instead — the dump is intentionally gone.)

- [ ] **Step 5: Typecheck + lint + full frontend suite**

Run: `cd frontend && npx tsc -b && npx oxlint src/screens/AssetDetailScreen.tsx && npx vitest run --pool=forks`
Expected: clean; full suite green.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/screens/AssetDetailScreen.tsx frontend/src/screens/AssetDetailScreen.test.tsx
git commit -m "feat(asset-detail): collapse Relationships containment to a one-line link, not a column dump"
```

---

## Self-Review

**Spec coverage (§5b, §5a P1a, §8 P1a):**
- "Metadata tab stops rendering known-author values as unattested" → Tasks 1 + 2. ✓
- "promoting `_evidence_section` into `effective_metadata`'s fallback" → Task 1 (`evidence_provenance`). ✓
- "Relationships tab … demote containment to a one-line link" → Task 3. ✓
- "surface evidence provenance (source attested / AI proposed / rulebook proposed)" → Task 1 label map. ✓
- AI-independent, no tier/async/migration → honored (Global Constraints). ✓
- NOTE (deferred, not P1a): surfacing D4 semantic *candidates* and pending joins in the Relationships tab depends on why `relationships.semantic.candidates` is empty for `cif_id` (a separate query/data investigation) — out of P1a scope; the section still renders whatever the backend returns.

**Placeholder scan:** no TBD/TODO; every code step shows real code; test bodies concrete. ✓

**Type consistency:** `evidence_provenance: string | null` defined in Task 1 (backend key) and Task 2 (`api.ts` type), consumed by `attestedByLabel` in Task 2; `attestedByLabel` used in `AuthorityBadge` + `MetadataTab`. Containment shape unchanged in Task 3. ✓
