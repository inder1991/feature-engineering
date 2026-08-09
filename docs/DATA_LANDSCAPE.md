# Enterprise Data Landscape

This document describes the shared bank data platform and the conventions that
govern interpretation of all source-system and data-product catalogs.

## Source systems and operational ownership

Business systems own their operational databases and remain the origin of their
data. Examples include:

| Source system | Primary business purpose |
| --- | --- |
| Finacle | Core banking: customer, account, balance, transaction, product, and service-outlet processing. |
| Way4 | Credit-card management and card-processing operations. |
| Other source systems | Domain-specific operational applications that own their own databases and business processes. |

The source system is the authority for its operational data. Its database is
optimized for transaction processing, not necessarily for enterprise-wide
analytics or reporting.

## ODS and EDP architecture

The bank ingests source data into two complementary environments for most
systems:

```text
Source-system database
        ├── ODS (Oracle) ── operational reporting / recent data
        └── EDP (Hadoop / Hive) ── enterprise analytics / historical data
```

### ODS

The Operational Data Store (ODS) is maintained in Oracle and serves operational
reporting requirements. It retains a limited rolling history of approximately
90 days. Use ODS for recent operational views and short look-back analysis; do
not assume it contains the complete history required for analytics or model
development.

### EDP

The Enterprise Data Platform (EDP) is the Hadoop/Hive-based data lake. It
ingests data from source systems for common enterprise use and is the platform
for historical data, broad analytics, long look-back windows, cohort analysis,
and backtesting. Hive should be used where complete history is required.

The hand-off between ODS and EDP/Hive must be reconciled when combining the two
sources. Validate the overlap and deduplicate events at the retention boundary.

## Data-vault and data-product layers

| Layer | Meaning | Availability / role |
| --- | --- | --- |
| `RDV` | Raw Data Vault / raw source-aligned ingestion | Present in both ODS and Hive. Data is mirrored from source systems through truncate-load or incremental ingestion with minimal transformation. It supports traceability and reconciliation. |
| `TDV` | Transformed Data Vault | Available only in Hive. It holds transformed and integrated vault data for enterprise analytical processing. |
| `SA` | Source Aligned | Applies limited corrections/fixes and may merge one or two raw sources while staying close to source semantics. |
| `EC` | Enterprise Curated | Applies enterprise business logic and more denormalized modelling to provide reusable enterprise concepts. |
| `BO` | Business Oriented | Downstream product tailored to the reporting or consumption needs of a particular business unit. |

`SA` and `EC` products can be reused by multiple `BO` schemas. A `BO` product
must not automatically be treated as an enterprise-wide canonical source,
because its population and logic may be deliberately specific to its consuming
business unit.

`DPL` means **Data Product Layer**: curated data products built from raw source
data and governed by their respective business teams. Published definitions,
eligibility rules, refresh practices, and quality controls are business-governed
data contracts.

## Entity and corporate-schema scope

Schema/table identifiers can contain an entity code. Where present, the data is
filtered or scoped to that entity:

| Entity code | Entity / geography |
| --- | --- |
| `UAE` | United Arab Emirates |
| `EIB` / `EI` | Emirates Islamic |
| `EGY` | Egypt |
| `SGP` | Singapore |
| `LON` | London |
| `IND` | India |

`CIB` denotes the bank's **Corporate schema**. All `BO_DPL_CIB` tables are
filtered to corporate customers only; they must not be interpreted as a complete
retail-and-corporate population. Preserve entity scope in any cross-product
analysis and do not assume entity-specific schemas represent bank-wide data.
