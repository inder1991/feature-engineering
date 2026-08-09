# CPR Data Catalog Description

This guide describes the Customer Profitability Report (CPR) finance-reporting
fact table and how to use it with the existing CIB, Finacle, and FTR catalogs.
For shared source-system, ODS, EDP/Hive, data-layer, and entity conventions,
see [DATA_LANDSCAPE.md](DATA_LANDSCAPE.md).

## Table summary

| Attribute | Description |
| --- | --- |
| Catalog/source | `CPR_FACT_CPR_YTD_DAILY_Column_Mapping.csv` |
| Physical table | `dpl_uae_hyperian.fact_cpr_ytd_daily` |
| Platform | Oracle ODS |
| Entity scope | UAE, as indicated by `dpl_uae` |
| Customer population | CIB corporate customers only, at present |
| Table role | Finance reporting fact table |
| Business subject | Customer and account profitability, balances, income, and credit-risk measures |
| Currency | AED for the explicitly suffixed AED measures |

`fact_cpr_ytd_daily` is a backend Finance reporting table that attributes the
bank's income, balances, risk, and expected-credit-loss measures to customer
and account records. It carries reporting dimensions for the PSM hierarchy
(`level_1`–`level_3`), legal entity, natural account, product, relationship
manager, and KPI reporting period. **Its current population is limited to CIB
corporate customers.** Model it as a corporate-customer profitability fact, not
as a bank-wide retail-and-corporate profitability population.

## Grain and time semantics

The logical fact grain is customer (`cust_num`), account (`acct_num`), finance
reporting dimensions, business date (`warehousestatus_key`), and KPI period
(`kpi_name`). `warehousestatus_key` is the authoritative CPR business-date
column and must be retained in every join, aggregation, and point-in-time
feature. Confirm its physical data type and reporting-calendar interpretation
before building production SQL.

The table has mixed time granularity:

- Previous months are reported at monthly granularity.
- The current month is reported at daily granularity.

For a single-period or point-in-time report, select the appropriate
`warehousestatus_key` snapshot. For historical features, rows may be aggregated
across multiple business dates after selecting a leakage-safe look-back window.
For example, `avg_casa_balance_last_12_months` is calculated from the relevant
12 monthly CASA snapshots. `kpi_name` is a reporting-period measure label, not
a substitute for the business date: `MTD`, `YTD`, `QTD`, `PMTD`, `PQTD`, and
`PYTD` must be compared only for the same defined business date and hierarchy
scope.

### KPI-period interpretation

`kpi_name` identifies the Finance reporting accumulation basis: `MTD`, `QTD`,
`YTD`, `PMTD`, `PQTD`, or `PYTD`. Its treatment depends on whether the measure
is a stock or a flow.

| Measure type | Examples | KPI-period rule |
| --- | --- | --- |
| Point-in-time stock | `eom_balance_aed`, `eom_avg_balance_aed`, `rwa`, `ecl`, `non_funded_balance_aed` | For the same `warehousestatus_key`, the balance value is the same economic snapshot for `MTD`, `QTD`, and `YTD`; the KPI label does not create a new balance. Select one approved label or deduplicate before aggregation. |
| Period flow | `total_income`, `total_nii`, `total_nfi` | Values are populated according to their period label: MTD is month-to-date, QTD is quarter-to-date, and YTD is year-to-date. They are cumulative within the named period. |

## Measures and analytical meaning

| Measure | Meaning and use |
| --- | --- |
| `eom_balance_aed` | Point-in-time end-of-month balance; use for closing-balance and exposure features. |
| `eom_avg_balance_aed` | Monthly average of daily EOD balances; use for average-balance and income-yield analysis. |
| `total_income` | Total income in AED attributed to the CPR record. |
| `total_nii` | Net interest income in AED. |
| `total_nfi` | Net fee income in AED. |
| `rwa` | Risk-weighted assets; use for risk-adjusted profitability analysis. |
| `ecl` / `ecl_stage` | Expected credit loss and IFRS 9 Stage 1/2/3 classification; use for credit-risk segmentation. |
| `non_funded_balance_aed` | Non-funded contingent exposure, including guarantees, in AED. |

Do not assume `total_income = total_nii + total_nfi` until Finance confirms the
income-component and allocation policy; total income may include additional
components or adjustments.

## Product-level profitability and balance analysis

CPR calculates CIB customer profitability at product and sub-product level by
grouping on `cust_num`, `prduct_cd`, `level_1`, `level_2`, `level_3`,
`warehousestatus_key`, and `kpi_name`. It can be used to report the income,
NII, NFI, balances, RWA, and ECL attributed to each product held by a customer.

Illustrative PSM hierarchy examples include the following. These labels explain
the intended hierarchy and must be validated against the approved Finance PSM
reference mapping before use in production.

| Hierarchy level | Example classifications | Typical analysis |
| --- | --- | --- |
| `level_1` | CASA, Core Lending, Retail Assets, Structured MM | Senior product-family profitability and risk reporting. |
| `level_2` | CASA sub-products, Term Loans, Retail-asset sub-products, Structured MM sub-products | Product-family analysis, such as term-loan income and exposure. |
| `level_3` | Product or sub-product detail | Granular product contribution and customer product-mix analysis. |

For CASA, use `eom_balance_aed` for closing balance and
`eom_avg_balance_aed` for the month-average balance based on daily EOD balances.
For fixed deposits, lending, FX, and other product families, filter by the
approved `prduct_cd` and PSM hierarchy mapping, then analyse `total_income`,
`total_nii`, `total_nfi`, balances, RWA, and ECL as applicable. The catalog
does not declare a separate FX-income field; FX profitability is analysed using
the income measures for rows classified to the Finance-approved FX product or
sub-product hierarchy.

### Feature aggregation rules

When building features across multiple `warehousestatus_key` values, choose the
aggregation based on the measure's time meaning:

| Measure type | Examples | Required aggregation across business dates | Example feature |
| --- | --- | --- | --- |
| Point-in-time stock | `eom_balance_aed`, `eom_avg_balance_aed`, `rwa`, `ecl`, `non_funded_balance_aed` | **Average (`AVG`)**, after selecting one compatible KPI label | `avg_casa_balance_last_12_months` |
| Period flow using non-overlapping values | `total_income`, `total_nii`, `total_nfi` with `MTD` or derived monthly increments | **Sum (`SUM`)** | `total_nii_last_12_months` |
| Cumulative period flow | `total_income`, `total_nii`, `total_nfi` with `QTD` or `YTD` | Use the final snapshot for the period or calculate the incremental difference; **do not sum cumulative rows across dates** | `ytd_total_income_as_of_date` |

Apply these rules only after filtering to one compatible `kpi_name`, product/
PSM scope, natural account (`na`), legal entity, and account/customer grain. Do not mix monthly
historical rows with multiple daily current-month rows without first normalizing
the current month to the intended monthly reporting snapshot; otherwise daily
records can overweight the feature window.

## Join summary

| From CPR | To catalog/table | Join | Expected relationship | Guidance |
| --- | --- | --- | --- |
| `cust_num` | `BO_DPL_CIB.BO_CIB_CUSTOMER.cust_num` | `CPR.cust_num = CIB.cust_num` | Customer 1:N CPR facts | CPR and CIB are both corporate-customer scoped. Use a CIB customer snapshot valid on or before the CPR report date, and profile any unmatched rows as data-quality exceptions. |
| `acct_num` | `rdv_finacle_uae_tbaadm.general_acct_mast_table.foracid` | `CPR.acct_num = GAM.foracid` | Account 1:N CPR facts over reporting dates/KPIs | `foracid` is the actual account number. Preserve formatting and validate population and entity alignment. |
| `GAM.acid` | `rdv_finacle_uae_tbaadm.eod_acct_bal_table.acid` | `GAM.acid = EOD.acid` | GAM 1:N EOD balance versions | This is the technical account relationship. Select EOD balance via its Type 2 validity dates before comparing it to CPR balances. |
| `le`, `prduct_cd`, `rm`, `level_1`–`level_3` | Relevant reference dimensions | Code-to-dimension join | Many CPR facts to one reference member | Candidate joins only until approved reference catalogs and code mappings are available. |

## Feature-engineering use and controls

CPR supports finance-focused customer and account features: income trends,
net-interest versus fee-income mix, balance and average-balance trends,
risk-adjusted income using RWA, ECL movement and stage migration, product/
PSM/RM portfolio segmentation, and non-funded exposure ratios. Use CPR only
for CIB corporate-customer analysis until its population is formally expanded.

Use reporting snapshots available at or before the prediction time. Do not use
future daily current-month CPR updates to score earlier dates, and do not
double-count a monthly row and its daily current-month counterparts. All income,
balance, and risk aggregations must retain the applicable `kpi_name`,
`warehousestatus_key`, legal entity, and hierarchy scope.
