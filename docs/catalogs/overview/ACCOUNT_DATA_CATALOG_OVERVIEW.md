## 1. High-level description

The Finacle General Account Master (GAM) is the account-level operational
dataset. Each record describes a bank account and its core identifiers,
customer association, product/scheme, service outlet, account lifecycle and
status, currency, balances, limits, transaction activity dates, and processing
controls. It is the authoritative account-master representation for the UAE
Finacle source in this catalog.

For ontology purposes, model this table as the **Account** entity. `acid` is
the technical Finacle account identifier and the mandatory key for the GAM to
end-of-day balance relationship; `foracid` is the customer-facing actual
account number. Customer ownership, product, branch, account status, monetary
position, and account lifecycle should be represented as distinct concepts
rather than collapsed into a generic account-detail attribute.


## 2. Account catalog

| Attribute | Description |
| --- | --- |
| Catalog/source | `Finacle_GENERAL_ACCT_MAST_TABLE_Column_Mapping.csv` |
| Physical table | `rdv_finacle_uae_tbaadm.general_acct_mast_table` (Finacle GAM) |
| Platform/layer | EDP/Hive Raw Data Vault (`RDV`) source-aligned account master |
| Entity scope | UAE, as indicated by `rdv_finacle_uae` |
| Business subject | Bank account / account master |
| Table role | Account dimension or account-master history |
| Intended grain | One row per Finacle account version; `acid` is the technical account key and `foracid` is the actual account-number key. Profile effective-date and active-record fields before treating either as unique across all history. |
| Primary entity | Account, linked to customer through `cif_id` (and `cust_id`, where its relationship is confirmed) |


### Feature-engineering use

GAM is the primary source for account-level profile and eligibility features.
Typical patterns include:

- account tenure, opening/closure, active/closed, frozen, and modification
  status features;
- product, scheme, scheme type/sub-type, product group, currency, branch, and
  ownership distributions;
- cleared and uncleared balance, lien, future-balance, drawing-power, and
  credit/overdraft-limit features;
- utilization ratios such as utilised versus sanctioned, discretionary, or
  drawing-power limits, subject to documented zero/negative-limit handling;
- transaction recency based on `last_tran_date`, `last_any_tran_date`, and
  last debit/credit transaction dates; and
- customer-level account portfolio features after linking GAM accounts to CIB.

GAM is an account master, not a transaction fact table. Use FTR for measured
transaction counts and amounts, and use GAM's last-transaction fields only as
account-state or recency attributes. For historical modelling, establish the
availability and versioning policy for `edp_effectivestartdate`,
`edp_modifiedts`, `edp_load_date`, `edp_activeind`, and `del_flg` before
joining an account record to a past prediction date. Do not assume the latest
master row describes the account as it existed historically.

## 3. CIB-Account relationship and join guidance

### Core relationship

The candidate cross-catalog customer bridge is:

```text
CIB Customer (`BO_CIB_CUSTOMER`)                     Finacle Account (`GENERAL_ACCT_MAST_TABLE`)
cust_num  ---------------------------------------->  cif_id
business_dt (customer snapshot)                        account-master effective/availability date

Customer (one)  <------------------------------  Account (many)
```

`CIB.cust_num = GAM.cif_id` is the candidate business-key join: the mapping
defines `cif_id` as the Finacle CIF identifier associated with the account.
Semantically, one customer can own many accounts, while an account normally
resolves to one primary CIF. `acct_ownership` must be profiled before assuming
this is the complete legal ownership model; joint holders or role-specific
owners may require a separate relationship source.

`foracid` is the candidate account-number bridge to CPR:

```text
CPR (`FACT_CPR_YTD_DAILY`)                            Finacle Account (`GENERAL_ACCT_MAST_TABLE`)
acct_num  ---------------------------------------->  foracid

CPR fact (many reporting dates/KPIs)  ------------>  Account (one)
```

The CPR overview also identifies `GAM.acid = EOD.acid` as the technical bridge
to `rdv_finacle_uae_tbaadm.eod_acct_bal_table`. Preserve account-number
formatting, including leading zeros, for `foracid` joins. A direct FTR-to-GAM
account join is not declared by the available mappings and must not be inferred
from similarly named account fields without an approved relationship.

### Point-in-time join

Do not join every account-master version to every historical CIB snapshot for
the same customer. For a feature date `t`, use:

1. a GAM record available on or before `t`, selected using an approved
   effective-date and active/deleted-record policy; and
2. the CIB row where `CIB.cust_num = GAM.cif_id` and `CIB.business_dt` is the
   latest snapshot date less than or equal to the GAM record's applicable date
   (or `t` for a scoring-date enrichment).

The mapping contains EDP effective, modified, load, and active-indicator
metadata, but does not by itself declare a complete Type 2 validity rule.
Confirm the end-date/overwrite convention with the data owner before using GAM
as an as-of dimension. If only a current account-master extract is available,
use it only when its availability is appropriate for the modelling date;
otherwise it can leak future account status, balance, or limit values.

### Relationship validation before production use

Treat these bridges as **candidate relationships that must be profiled**, not
as enforced database foreign keys. Verify that `acid` and `foracid` are unique
at the selected account version, identifiers use compatible formats (especially
leading zeros), and `cif_id` match rates to `CIB.cust_num` are acceptable.
Inspect accounts with null or unmatched CIFs, multiple current master records,
logical-delete flags, and unexpected ownership classifications. Preserve
unmatched accounts unless the business purpose explicitly permits excluding
them; they can represent closed, system-only, pooled, or data-quality cases.

## 4. Ontology summary

| Ontology concept | CIB representation | Account representation | Relationship |
| --- | --- | --- | --- |
| Customer | `cust_num` and customer-profile attributes | `cif_id` customer reference | `cust_num` <-> `cif_id` |
| Customer state over time | `business_dt` snapshot | Account-master effective/availability metadata | As-of enrichment after version policy is confirmed |
| Account | Not the primary entity | `acid`, `foracid`, account master attributes | One customer has many accounts |
| Account identity | Customer context only | `acid` technical key; `foracid` actual account number | `acid` to EOD; `foracid` to CPR `acct_num` |
| Account lifecycle and controls | Customer status/relationship context | Opening/closure, active/delete, freeze, operating mode, transaction controls | Account-state attributes for modelling |
| Monetary position and capacity | Customer-level profitability context | Cleared/uncleared, lien, future balances, limits, drawing power, utilization | Account-level exposures and capacity |

The resulting feature model can be customer-centric or account-centric. For a
customer model, first select leakage-safe GAM versions, aggregate account
attributes by `cif_id`, then join to the matching CIB snapshot through
`cust_num` and `business_dt`. For an account model, retain `acid` as the
technical key and `foracid` as the business-facing account-number key so that
balances and CPR facts can be joined with their declared identifiers.
