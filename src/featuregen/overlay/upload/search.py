from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from collections.abc import Iterable

from psycopg.rows import dict_row

from featuregen.overlay.upload.read_scope import allowed_sensitivities

_SQL = """
SELECT n.object_ref, n.table_name, n.column_name, n.kind, n.data_type, n.definition,
       n.is_grain, n.is_as_of, n.catalog_source, n.concept, n.domain, n.sensitivity,
       n.additivity, n.unit, n.currency, n.entity,
       ts_rank_cd(n.search_doc, plainto_tsquery('english', %(q)s))
         + (CASE WHEN n.is_grain THEN 0.5 ELSE 0 END)
         + (CASE WHEN n.is_as_of THEN 0.3 ELSE 0 END) AS score
FROM graph_node n
JOIN overlay_drift_watermark w ON w.catalog_source = n.catalog_source
WHERE n.search_doc @@ plainto_tsquery('english', %(q)s)
  AND w.last_completed_at >= %(cutoff)s
  AND (n.sensitivity IS NULL OR n.sensitivity = ANY(%(allowed)s))   -- read-scope hard filter
ORDER BY score DESC
LIMIT %(limit)s
"""


@dataclass(frozen=True, slots=True)
class SearchHit:
    object_ref: str
    table: str
    column: str | None
    kind: str
    data_type: str | None
    definition: str | None
    is_grain: bool
    is_as_of: bool
    catalog_source: str
    concept: str | None
    domain: str | None
    sensitivity: str | None
    additivity: str | None
    unit: str | None
    currency: str | None
    entity: str | None
    score: float


def search(conn, query: str, *, now: datetime, roles: Iterable[str] = (),
           fresh_within: timedelta = timedelta(hours=24), limit: int = 20) -> list[SearchHit]:
    cutoff = now - fresh_within
    allowed = allowed_sensitivities(roles)   # sensitivity classes these roles may see
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SQL, {"q": query, "cutoff": cutoff, "limit": limit, "allowed": allowed})
        rows = cur.fetchall()
    return [SearchHit(
        object_ref=r["object_ref"], table=r["table_name"], column=r["column_name"],
        kind=r["kind"], data_type=r["data_type"], definition=r["definition"],
        is_grain=r["is_grain"], is_as_of=r["is_as_of"], catalog_source=r["catalog_source"],
        concept=r["concept"], domain=r["domain"], sensitivity=r["sensitivity"],
        additivity=r["additivity"], unit=r["unit"], currency=r["currency"], entity=r["entity"],
        score=float(r["score"])) for r in rows]
