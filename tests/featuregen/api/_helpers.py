"""Stub-auth headers + a small deposits catalog CSV shared across the API tests."""
from datetime import UTC, datetime, timedelta

# ── the window every cost confirmation lives inside ─────────────────────────────────────────────
# `ExpiryWindow` (api.routes.code_generation_jobs) bounds `expires_at` on BOTH approval bodies to
# the platform's own triage window: a real ISO-8601 instant, strictly in the future, at most 168
# hours out — the same 1..168 `MethodOverrideIn.expires_in_hours` already states for the other
# approval a person gives.
#
# ▲ SO A FIXED CALENDAR DATE IS A FIXTURE THAT EXPIRES. Every approval fixture read
# `2026-12-31`, which was ~129 days out when the bound landed (refused) and would have been in
# the PAST a year later (refused, differently). Three days out is comfortably inside the bound
# and comfortably clear of the UTC-midnight FLOOR `canonical_approval_expiry` applies, so the
# same-day replay tests still get two distinct instants that floor to one recorded expiry.
_APPROVAL_DAY = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%d")

#: The expiry every approval fixture confirms with.
APPROVAL_EXPIRES_AT = f"{_APPROVAL_DAY}T09:00:00Z"

#: A DIFFERENT instant on the SAME UTC day — what the day-flooring replay tests are made of.
APPROVAL_EXPIRES_AT_SAME_DAY = f"{_APPROVAL_DAY}T17:00:00Z"

#: The UTC midnight both of those floor to, spelled the way the store reads it back.
APPROVAL_EXPIRY_FLOOR = f"{_APPROVAL_DAY} 00:00:00"


def hours_from_now(hours: float) -> str:
    """An ISO-8601 instant `hours` from now.

    The expiry-window boundary tests describe a DISTANCE from now, never a date: `169` has to stay
    one hour outside the bound for ever, and a literal that meant that in August means something
    else in December.
    """
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")

# Default caller: broad FUNCTIONAL access (all of catalog/feature/iam) but NO data-sensitivity role,
# so read-scope tests still hide pii/restricted. platform_admin via the stub is authenticated=False,
# so admin routes (which additionally require authenticated) still 403 under this header.
AUTH = {"X-User": "tester", "X-Roles": "platform_admin"}
PII_AUTH = {"X-User": "tester", "X-Roles": "platform_admin,pii_reader"}

# Role-scoped stubs for authorization-boundary tests.
VIEWER = {"X-User": "v", "X-Roles": "catalog_viewer"}      # read-only
OWNER = {"X-User": "o", "X-Roles": "data_owner"}           # upload/curate, no feature workflow
ENGINEER = {"X-User": "e", "X-Roles": "feature_engineer"}  # feature workflow, no upload

DEPOSITS_CSV = """\
source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,additivity,unit,currency,entity
deposits,accounts,id,integer,y,,account primary key,,,,,,,Account
deposits,accounts,posted_at,timestamp,,y,posting timestamp,,,,,,,
deposits,accounts,balance,numeric,,,end-of-day ledger balance,,,,semi_additive,dollars,USD,Account
deposits,accounts,cust_id,integer,,,owning customer,,customers.cust_id,N:1,,,,Customer
deposits,customers,cust_id,integer,y,,customer primary key,,,,,,,Customer
deposits,customers,email,text,,,customer contact email,pii,,,,,,Customer
deposits,transactions,txn_id,integer,y,,transaction primary key,,,,,,,Transaction
deposits,transactions,account_id,integer,,,posting account,,accounts.id,N:1,,,,Account
deposits,transactions,amount,numeric,,,signed transaction amount,,,,additive,dollars,USD,Transaction
"""


def upload_csv(client, source: str, text: str, headers=AUTH):
    return client.post(
        "/uploads",
        data={"source": source},
        files={"file": (f"{source}.csv", text.encode(), "text/csv")},
        headers=headers,
    )
