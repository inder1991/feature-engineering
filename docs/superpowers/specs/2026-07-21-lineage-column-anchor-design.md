# Lineage graph: column-anchor redesign (problem + fix)

Date: 2026-07-21 · Status: fix in progress on `feature-ready-ingestion` · Owner surface: `frontend/src/screens/LineageView.tsx`

## 1. What the screen is

From Search, every column result carries a **Graph** action. It navigates to the lineage view
(`LineageView.tsx`, reactflow canvas) anchored on the clicked ref, fetching
`GET /graph/lineage?ref=<table-or-column>&source=<src>&direction=both&depth=1&layers=joins,entity,features`.
The intended picture: table cards connected by governed relationship edges — **joins** (VERIFIED
`approved_join` only), **entity bridges**, and **feature lineage** (column → feature → consumers) —
with each table's columns rendered as rows inside its card.

## 2. The problem (observed 2026-07-21, first real-file demo)

Clicking Graph on any column of the freshly ingested FTR table
(`public.comp_financial_tran_repos_dly`, 127 columns) renders:

- **one gigantic table card listing all 127 column rows**, hanging off the right edge of the viewport,
- an otherwise **blank canvas** (nothing else to draw),
- the layers legend floating over empty space.

User verdict: *"one box with 127 entries, horrible."* Correct verdict.

### Root causes (verified in code)

| # | Cause | Where |
|---|-------|-------|
| 1 | Columns render **only** as rows inside their owning table's card; the clicked column is never its own node — the anchor unit is always the **table** (`anchorUnitId = <src>:public.<table>`), the column is merely highlighted as `matchId` inside it. | `LineageView.tsx` ~205 (rows), ~426 (anchor unit) |
| 2 | The anchor table arrives **expanded** from the depth-1 fetch and there is **no cap** on rendered column rows. 127 columns → a 127-row tower. Collapse exists but is opt-in per click. | `TableCard`, `columns.map(...)` |
| 3 | The **zero-edges state was never designed.** With no drawable edges the canvas is simply blank around the tower — no explanation, no next action. | `drawnEdges`/`visibleUnits` produce an empty set; nothing renders for it |
| 4 | The anchor is **not centered**; fitView leaves the single card at the viewport edge. | canvas init |

### Why the demo hits this state specifically

The relationship layers are all *governed*: joins draw only after **two distinct platform-admins
approve** a proposal; entity bridges only after entity assignment is **verified**; feature lineage
only after features exist. A day-one catalog (one table, nothing approved — and, until today, a dead
LLM key meaning not even proposals existed) therefore has **zero drawable edges by construction**.
The screen was designed for the mature state and given no design for the honest starting state.

### The design principles it violates

- **The subject of the question should be the center of the answer.** The user asked about a
  *column*; the screen answered about its *table*.
- **Authority-honest UI explains absence.** Everywhere else the product says *why* something is
  empty and where to act (readiness tab, governance queues). The graph just showed void.
- **Never render unbounded lists.** A card must not scale linearly with catalog width.

## 3. The fix

### 3.1 Column-centric anchor (the user's design, confirmed in dialogue)

When the anchor is a column, render it as its **own node at the visual center** — kind chip
`column`, name, concept when known — with a quiet structural **"belongs to" edge** to its table's
card. The table card renders **compact**: name + `127 columns` count, collapsed by default,
expandable on demand. Relationship edges whose endpoint is the anchored column attach to the column
node itself. Table anchors keep the existing table-card-centric layout.

Concrete target for `cif_id` (customer information file identifier — the most connective column in
an FTR table):

```
                       ┌ table ───────────────────────┐
                       │ comp_financial_tran_repos_dly │
                       │ 127 columns           [expand]│
                       └───────────────┬───────────────┘
                                       │  belongs to
                  ┌ column ────────────┴───────────────┐
                  │ cif_id                             │
                  │ Customer Information File ID       │
                  │ Compliance · customer identifier   │
                  └───┬──────────────┬─────────────┬───┘
               joins  │       entity │   features  │
                      ▼              ▼             ▼
        cust_master.cif_id    "customer"     txn_count_per_
        (approved join)       entity bridge  customer_30d → consumers
```

### 3.2 Cap every expanded table card

At most **8 column rows** when expanded, priority: matched/anchored column, then columns that are
endpoints of visible edges, then the rest. Below the cap, a **"+N more columns"** control expands
the full list *inside* the card with `max-height` + internal scroll. A card never exceeds roughly
viewport height, regardless of catalog width.

### 3.3 Honest empty state

When the drawable-edge set (for the current layer toggles) is empty, render an explanatory panel
beside the graph, one plain-English sentence per toggled-on layer, each linking to the screen where
that layer gets created:

- *"No approved joins reach this column yet. Join proposals are reviewed on the Governance screen."*
- *"No verified entity bridge yet. Entity assignments are confirmed on the Governance screen."*
- *"No features are derived from this column yet. Features are created in the Workbench."*

No fabricated counts (the lineage endpoint does not report proposal counts). The moment governance
approves the first join, the sentence is replaced by the first real edge.

### 3.4 Center the anchor

fitView/setCenter on the anchor node (column node when column-anchored, table card otherwise).

## 4. What does not change

The details drawer, the feature-trace interaction, the expand-neighbors chips, layer toggles, the
edge a11y list, and the table-anchored layout all keep their current behavior. Scope is
`LineageView.tsx`, its test file, and the `ln-*` stylesheet only. No backend change: the lineage
endpoint already returns everything needed.

## 5. Verification

TDD (tests written failing-first, in `LineageView.test.tsx`):
1. column anchor → column node exists + containment edge + compact table card (no 127 rows);
2. 127-column card renders ≤8 rows + "+119 more columns" expander revealing a scrollable full list;
3. zero drawable edges → the per-layer empty-state sentences render and respect layer toggles;
4. regression: all existing multi-node/table-anchor tests pass unmodified.

Gate: full `npx vitest run --pool=forks` green, `tsc -b` clean, oxlint clean on touched files.
Deploy: rebuild `featuregen-frontend:local`, `kind load`, rollout restart, verify on the running
demo (`http://localhost:8080`) by clicking Graph on `cif_id`.
