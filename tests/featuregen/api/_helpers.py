"""Stub-auth headers + a small deposits catalog CSV shared across the API tests."""

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
