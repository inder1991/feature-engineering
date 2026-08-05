# Graph mode — visual gap against the artifact

Source of truth: `docs/design/graph-lineage-experience-concept.html`, compared against the
deployed build at commit `2c9…` (twelve commits on `feature/asset-detail-port`).

Written from a side-by-side of the artifact and the running page. Structure now largely
matches; **appearance does not**. The remaining work is node/edge rendering and the inspector,
not layout.

## 1. Node cards — the biggest visual difference

| | Artifact | Build |
| --- | --- | --- |
| Card fill | white / `--surface` | white ALREADY — the amber came from `.ln-note` (58px band) and `.ln-card--stale .ln-head`, both removed in `242e1380`, which postdates the screenshot. Verify before re-fixing. |
| Stale marker | small grey dot + lowercase `stale` in the header | uppercase `STALE` badge chip |
| Meta line | one compact grey line: `cib · BO_DPL_CIB · 85 columns` | source + separate chips, taller |
| Column row | white row, name left, one right-aligned chip (`ANCHOR`, `AS-OF`, `GRAIN`, `MAPPED`) | similar but inside a tinted card |
| Expander | none floating | `+` FAB **overlapping the card's right edge** |
| Anchor card | teal header `COLUMN cust_num  GRAIN`, body `Customer Number · customer_id varchar(150) · source declared` | teal header, body shows only `customer_id` |

Remaining after `242e1380`: the stale marker is still an uppercase `STALE` badge where the
artifact uses a 6px grey dot + lowercase word; the meta line is not condensed to one grey row;
and the `+` expander still floats over the card's right edge.

## 2. Edge labels

Artifact draws each label as a **pill on the line**: `belongs to` (grey outline, white fill),
`customer · strong` (amber outline). The build draws bare text with no background, which
collides with nodes and clips. Needs a chip background, not a longer clip budget.

## 3. Zoom controls

Artifact: a compact vertical `+ / − / #` stack inside the canvas, top-right. Build: ReactFlow's
default `Controls` bottom-right.

## 4. Inspector — still structurally wrong (was B4)

Artifact order, none of which the build renders:

1. `SELECTED COLUMN` micro-label + `SOURCE ATTESTED TERM` badge on one row
2. `cust_num` as a sans heading (not the mono full ref)
3. Definition prose
4. Pill row: `CUSTOMER_ID · AI PROPOSED`, `VARCHAR(150)`
5. **2×2 fact grid**: Business term / Domain / Grain use / Join use
6. `CROSS-CATALOG CAPABILITY` — tinted card, title, prose, two quiet chips
7. `RECOMMENDED FEATURES USING THIS COLUMN`

There is **no Close button** in the artifact's inspector. The build opens with one, floating
above a gap, and shows the mono object ref where the artifact shows a short name.

## 5. Recommendations copy

Artifact: plain English — "Count distinct products held by customer.", "Age of the customer
relationship." Build: the raw recipe formula
(`product_breadth_90d(cust_smart_cust_pkg_cd, …)`). Needs a description field from the
suggestion payload; if none exists, that is a backend gap to state, not prose to invent.

## 6. Context bar

Close. Artifact badges are `CUSTOMER IDENTIFIER` and `2 CHECKS REQUIRED`; the build omits the
second because readiness data is not passed to this component. Either thread readiness in or
leave it out — do not fabricate a count.

## Suggested order

1. Node card restyle (§1) — largest visual gain, contained to the node components.
2. Edge label pills (§2).
3. Inspector rebuild (§4) — largest content gain, needs the `Drawer` component restructured.
4. Recommendation copy (§5) — check the payload for a description field first.
5. Zoom controls (§3).

## Standing caution

Nine deploys in this session were "verified" by grepping the served bundle for strings. That
cannot see layout, spacing, colour or overlap, and it missed every defect on this page. Verify
graph changes with a rendered screenshot before reporting them as done.
