## 1. High-level description

FTR is the transaction-level operational dataset. Each record represents a
financial transaction or transaction event and supplies the measurable facts
and operational context of that event. Its attributes typically cover the
transaction identifier, customer identifier, business/processing dates,
amounts and currencies, debit/credit or direction indicators, transaction
type/channel, account or counterparty references, location/branch, and
processing, reversal, or status flags.

For ontology purposes, model this table as the **FinancialTransaction** entity
or event. It connects a customer to a monetary movement and related processing
context. Amount, currency, direction, channel, counterparty, account, and
event-time concepts should be represented as distinct concepts rather than
collapsed into a generic "transaction detail" attribute.


## 2. FTR catalog

| Attribute | Description |
| --- | --- |
| Catalog/source | `FTR_Column_Mapping-6.csv` |
| Physical table | `DPL_EIB.TRAN_REPOS` (FTR transaction repository) |
| Business subject | Financial transaction / transaction event |
| Table role | Transaction fact table |
| Intended grain | One row per transaction; `tran_id` is the expected unique business key. |
| Primary entity | Financial transaction, linked to customer through `cif_id` |


### Feature-engineering use

FTR is the primary source for behavioral features. Typical patterns include:

- transaction count, value, average, minimum/maximum, and volatility over
  rolling time windows;
- debit/credit, currency, channel, transaction type, branch, and counterparty
  distributions;
- recency, frequency, monetary (RFM), burstiness, velocity, and time-between-
  transaction features;
- reversal/error/status rates and unusual-activity indicators;
- customer-level aggregation after linking transactions to CIB.

Use the event/business date in FTR as the feature cutoff driver, and include
only transactions available before the prediction timestamp. Reversal or
correction records should be handled according to a documented policy so that
they are not double counted.

## 3. CIB–FTR relationship and join guidance

### Core relationship

The intended cross-catalog customer bridge is:

```
CIB Customer (`BO_CIB_CUSTOMER`)                     FTR Transaction (`TRAN_REPOS`)
cust_num  ---------------------------------------->  cif_id
business_dt (customer snapshot)                        transaction/event business date

Customer (one)  <------------------------------  FinancialTransaction (many)
```

`CIB.cust_num = FTR.cif_id` is the candidate business-key join. Semantically,
one customer can have many FTR transactions, while each FTR transaction should
normally resolve to one customer record valid at its event date. Therefore the
expected analytical relationship is **Customer 1 : N FinancialTransaction**.

### Point-in-time join

Do not join every transaction to every historical CIB snapshot for the same
customer. For a transaction dated `t`, use the CIB row where:

1. `CIB.cust_num = FTR.cif_id`; and
2. `CIB.business_dt` is the latest snapshot date less than or equal to the FTR
   transaction/business date.

This is an as-of (slowly changing dimension) join. If the CIB export contains
only a current snapshot, use it only when its availability is appropriate for
the modelling date; otherwise it can leak future customer attributes.

### Relationship validation before production use

Treat the bridge as a **candidate relationship that must be profiled**, not as
an enforced database foreign key. Validate that identifiers use the same
format (including leading zeros), calculate FTR-to-CIB match rate, check the
uniqueness of (`business_dt`, `cust_num`) in CIB and `tran_id` in FTR, and
inspect unmatched `cif_id` values. Preserve unmatched transactions unless the
business purpose explicitly permits excluding them; they may represent unknown,
closed, external, or data-quality cases.

## 4. Ontology summary

| Ontology concept | CIB representation | FTR representation | Relationship |
| --- | --- | --- | --- |
| Customer | `cust_num` and customer-profile attributes | `cif_id` reference | `cust_num` ↔ `cif_id` |
| Customer state over time | `business_dt` snapshot | Event-time customer context | As-of enrichment |
| Financial transaction | Not the primary entity | `tran_id` and transaction attributes | One customer has many transactions |
| Monetary movement | Customer context only | Amount, currency, direction, transaction type | Transaction describes movement for a customer |
| Operational context | Customer branch/relationship/status | Channel, branch, counterparty, processing/reversal/status | Contextual attributes for modelling |

The resulting feature model is normally customer-centric: aggregate FTR events
by `cif_id` over leakage-safe windows, then join the results to the matching
CIB customer snapshot through `cust_num` and `business_dt`.