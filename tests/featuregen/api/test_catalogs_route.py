"""`GET /catalogs` — the catalog pick-list, end to end through the real route.

Before this route the Governance Review screen opened on an empty "source" text input and rendered
nothing until the operator guessed a slug (`cib`, `ftr`). This route offers them instead.

The interesting property is not the happy path, it is the EXISTENCE ORACLE: catalog-level visibility
does not exist in this system, so visibility is derived from the column-level predicate (migration
1032's `visible_requires <@ allowed_classes(roles)`). A caller who can see no column in a catalog must
not learn that the catalog exists — no name, no count, no row. Asserted as ABSENCE, and asserted with
the SAME seeding under a granting role so the absence is provably the scope predicate rather than an
empty fixture.

Roles come from the authenticated session, never the request body/query — the route has no way to be
asked for someone else's scope.
"""
from __future__ import annotations

import pytest
from tests.featuregen.api._helpers import AUTH, PII_AUTH, upload_csv

_HEADER = ("source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,"
           "additivity,unit,currency,entity\n")

# The live two-catalog shape. `cib` is fully open; `ftr` mixes one open column with one pii column so
# a count can be checked against what the caller may actually see.
CIB_CSV = _HEADER + (
    "cib,customers,cif_id,text,y,,customer key,,,,,,,Customer\n"
    "cib,customers,opened_on,date,,,relationship start,,,,,,,Customer\n"
    "cib,accounts,acct_id,text,y,,account key,,,,,,,Account\n"
)
FTR_CSV = _HEADER + (
    "ftr,transfers,tran_id,text,y,,transfer key,,,,,,,Transaction\n"
    "ftr,transfers,sender_name,text,,,ordering customer name,pii,,,,,,Customer\n"
)
# Every column pii-tagged: the catalog an unprivileged caller must not learn exists.
VAULT_CSV = _HEADER + (
    "vault,identities,eida_num,text,,,national id number,pii,,,,,,Customer\n"
    "vault,identities,passport_no,text,,,passport number,pii,,,,,,Customer\n"
)


@pytest.fixture
def three_catalogs(client):
    for source, csv in (("cib", CIB_CSV), ("ftr", FTR_CSV), ("vault", VAULT_CSV)):
        assert upload_csv(client, source, csv).status_code in (200, 201)
    return client


def _slugs(body) -> list[str]:
    return [c["source"] for c in body["catalogs"]]


def _by_source(body) -> dict:
    return {c["source"]: c for c in body["catalogs"]}


def test_offers_the_catalog_slugs_the_caller_can_see(three_catalogs):
    """The whole point: the screen no longer has to be told `cib` / `ftr`, it can ask."""
    res = three_catalogs.get("/catalogs", headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"catalogs"}
    assert "cib" in _slugs(body) and "ftr" in _slugs(body)
    assert _slugs(body) == sorted(_slugs(body))            # stable, slug-sorted


def test_a_catalog_with_no_visible_column_is_absent_from_the_response(three_catalogs):
    """THE EXISTENCE ORACLE. `vault` is pii top to bottom. Under the default (no data-sensitivity
    role) identity it must not appear AT ALL — not as a row with zero counts, and its slug must not
    appear anywhere in the payload."""
    open_res = three_catalogs.get("/catalogs", headers=AUTH)
    assert open_res.status_code == 200
    assert "vault" not in _slugs(open_res.json())
    assert "vault" not in open_res.text        # no name, no count, nowhere in the body

    # The SAME fixture under a pii_reader session DOES return it — so the absence above is the read
    # scope, not a seeding accident.
    priv = three_catalogs.get("/catalogs", headers=PII_AUTH).json()
    assert "vault" in _slugs(priv)
    assert _by_source(priv)["vault"] == {"source": "vault", "tables": 1, "columns": 2}


def test_counts_are_consistent_with_what_the_caller_may_see(three_catalogs):
    """`ftr` has two columns, one pii. The unprivileged caller is told 1; the pii_reader is told 2.
    A count over hidden columns would itself be a leak."""
    open_body = three_catalogs.get("/catalogs", headers=AUTH).json()
    assert _by_source(open_body)["ftr"] == {"source": "ftr", "tables": 1, "columns": 1}
    assert _by_source(open_body)["cib"] == {"source": "cib", "tables": 2, "columns": 3}

    priv_body = three_catalogs.get("/catalogs", headers=PII_AUTH).json()
    assert _by_source(priv_body)["ftr"] == {"source": "ftr", "tables": 1, "columns": 2}
    assert _by_source(priv_body)["cib"] == {"source": "cib", "tables": 2, "columns": 3}


def test_counts_reconcile_with_the_search_browse_for_the_same_caller(three_catalogs):
    """Cross-surface consistency check: the column count must equal the number of column nodes the
    caller can browse in `/search` for that source — the two surfaces share one scope predicate, and
    a divergence means one of them is lying about scope."""
    listed = _by_source(three_catalogs.get("/catalogs", headers=AUTH).json())
    hits = three_catalogs.get("/search", headers=AUTH).json()["hits"]
    for source, entry in listed.items():
        cols = {h["object_ref"] for h in hits
                if h["catalog_source"] == source and h["kind"] == "column"}
        assert entry["columns"] == len(cols), source


def test_no_catalogs_yet_is_an_empty_list(client):
    res = client.get("/catalogs", headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"catalogs": []}


def test_the_route_never_ships_a_display_name(three_catalogs):
    """There is no display-name source of truth in this system. The response must carry the slug and
    nothing that looks like an authoritative prose label."""
    for entry in three_catalogs.get("/catalogs", headers=PII_AUTH).json()["catalogs"]:
        assert set(entry) == {"source", "tables", "columns"}


def test_the_route_never_ships_pending_counts(three_catalogs):
    """Pending governance counts belong to the queue surface (one implementation, no drift)."""
    body = three_catalogs.get("/catalogs", headers=PII_AUTH).text
    assert "pending" not in body


def test_requires_catalog_read(three_catalogs):
    """access_admin holds ONLY iam:manage — no catalog:read -> 403, mirroring the readiness peers."""
    res = three_catalogs.get("/catalogs", headers={"X-User": "a", "X-Roles": "access_admin"})
    assert res.status_code == 403
