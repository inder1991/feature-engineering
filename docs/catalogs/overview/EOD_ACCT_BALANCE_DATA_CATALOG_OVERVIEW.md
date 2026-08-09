## 1. High-level description

EOD Account Balance is the Finacle end-of-day account-balance history dataset.
Each record represents an effective version of an account's end-of-day balance
state, including the technical account key, effective date range, service
outlet, total current balance, cleared balance, and audit information. It is a
Type 2 balance history rather than a transaction-level ledger.

For ontology purposes, model this table as the **AccountBalanceSnapshot**
entity or state. It connects an account to its monetary position at a defined
end-of-day effective period. Account identity, balance date, total balance,
cleared balance, branch, and record-validity dates should be represented as
distinct concepts rather than collapsed into a generic balance-detail
attribute.


## 2. EOD account balance catalog

| Attribute | Description |
| --- | --- |
| Catalog/source | `Finacle_EOD_ACCT_BAL_TABLE_Column_Mapping.csv` |
| Physical table | `rdv_finacle_uae_tbaadm.eod_acct_bal_table` (Finacle EOD account balance) |
| Platform/layer | EDP/Hive Raw Data Vault (`RDV`) source-aligned account-balance history |
| Entity scope | UAE, as indicated by `rdv_finacle_uae` |
| Business subject | Account end-of-day balance / balance history |
| Table role | Type 2 account-balance snapshot fact or history table |
| Intended grain | One account-balance version per (`acid`, `eod_date`); this composite pair is declared as the primary key. |
| Primary entity | Account balance snapshot, linked to the Finacle account master through `acid` |


### Feature-engineering use

EOD Account Balance is the primary source for historical account-balance
features. Typical patterns include:

- current and cleared balance as-of a scoring date;
- average, minimum, maximum, volatility, and trend of balance across rolling
  end-of-day windows;
- balance change, growth, drawdown, and days-in-credit/debit position;
- cleared-versus-current balance difference and availability indicators;
- branch (`sol_id`) balance segmentation; and
- customer-level balance exposure after linking balance snapshots through GAM
  to CIB.

Use `eod_date` as the effective start of the balance version and
`end_eod_date` as its effective end. For a feature date `t`, select the record
where `eod_date <= t` and `end_eod_date > t`, subject to confirming the
source's inclusive/exclusive boundary convention. The mapping states that
active records use a far-future `end_eod_date`, such as 31-DEC-2099. Do not
sum overlapping Type 2 versions for an account; select one valid version per
account and end-of-day observation before aggregating across time.

## 3. Account-EOD relationship and join guidance

### Core relationship

The declared account bridge is:

```text
Finacle Account (`GENERAL_ACCT_MAST_TABLE`)            EOD Account Balance (`EOD_ACCT_BAL_TABLE`)
acid  --------------------------------------------->  acid
account-master effective/availability date               eod_date to end_eod_date validity period

Account (one)  <----------------------------------  AccountBalanceSnapshot (many)
```

`GAM.acid = EOD.acid` is the technical account-key join. One account can have
many end-of-day balance versions, while each EOD balance version resolves to
one account. `acid` is a technical key; use GAM's `foracid` when a
customer-facing account-number bridge is required, including the declared CPR
relationship (`CPR.acct_num = GAM.foracid`).

The customer path is therefore indirect:

```text
CIB Customer (`cust_num`)  -->  GAM Account (`cif_id`, `acid`)  -->  EOD Balance (`acid`)

Customer (one)  <---------------- Account (many) <---------------- Balance snapshots (many)
```

`CIB.cust_num = GAM.cif_id` remains a candidate business-key bridge that must
be profiled. Do not infer a direct EOD-to-CIB join: the EOD mapping contains no
customer identifier.

### Point-in-time join

For a balance feature as of date `t`:

1. select the EOD record whose Type 2 interval contains `t` (`eod_date <= t`
   and `end_eod_date > t`, after confirming boundary semantics);
2. join it to the GAM account with `GAM.acid = EOD.acid`, using a GAM version
   available on or before `t` under an approved master-history policy; and
3. if customer enrichment is required, select the latest CIB snapshot where
   `CIB.cust_num = GAM.cif_id` and `CIB.business_dt` is on or before `t`.

This sequencing prevents an earlier balance from being attributed to a later
account owner or customer profile. `lchg_time` is an audit timestamp, not a
replacement for the declared balance-validity dates.

### Relationship validation before production use

Treat the GAM-EOD bridge as a **candidate relationship that must be profiled**,
not as an enforced database foreign key. Verify uniqueness of (`acid`,
`eod_date`), check for overlapping or gapped validity intervals per `acid`,
confirm the far-future active-record convention, and calculate EOD-to-GAM match
rates. Also reconcile `cur_bal` and `clr_bal_amt` with their documented sign,
currency, and availability conventions before creating ratios or aggregating
across account populations. Preserve unmatched balance rows unless their
exclusion is explicitly justified; they may indicate account-master timing,
closure, or data-quality conditions.

## 4. Ontology summary

| Ontology concept | GAM account representation | EOD balance representation | Relationship |
| --- | --- | --- | --- |
| Account | `acid` technical key; `foracid` actual account number | `acid` account reference | `GAM.acid` <-> `EOD.acid` |
| Account balance snapshot | Account-master balance context only | `cur_bal`, `clr_bal_amt` | One account has many balance versions |
| Balance validity period | Master effective/availability metadata | `eod_date`, `end_eod_date` | Type 2 as-of selection |
| Service outlet | `sol_id` account branch context | `sol_id` balance-record branch | Branch context for reconciliation and segmentation |
| Customer exposure | `cif_id` customer reference | No direct customer identifier | EOD -> GAM -> CIB customer path |
| Audit trail | GAM change metadata | `lchg_user_id`, `lchg_time` | Record-change context, not feature event time |

The resulting feature model may be account-centric or customer-centric. For an
account model, select the leakage-safe EOD balance version by `acid` and its
validity interval. For a customer model, first select those balance versions,
join them to GAM through `acid`, aggregate across accounts by `cif_id`, and
then enrich the result with the matching CIB customer snapshot.
