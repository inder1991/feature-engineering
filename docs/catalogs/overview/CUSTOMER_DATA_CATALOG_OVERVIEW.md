### High-level description

`BO_CIB_CUSTOMER` is the corporate-customer reference dataset. It describes
who the corporate customer is and their relatively stable attributes: customer identity,
customer classification and segment, legal or demographic attributes, contact
and address information, relationship/branch attributes, onboarding and status
indicators, and other customer-master characteristics. Because the expected
key includes `business_dt`, it should be treated as an *as-of snapshot*
dimension rather than assumed to be one permanently current customer row.

For ontology purposes, model this table as the **Customer** entity, with
customer identity, customer profile, customer status, organizational/location,
and time-versioned customer-state concepts. `cust_num` is the principal
customer identifier within this catalog; `business_dt` expresses the effective
snapshot date.

## 1. CIB Customer catalog

| Attribute | Description |
| --- | --- |
| Catalog/source | `CIB_Customer_Column_Mapping-4.csv` |
| Physical table | `BO_DPL_CIB.BO_CIB_CUSTOMER` |
| Business subject | Customer / customer master |
| Table role | Customer dimension or periodic customer snapshot |
| Intended grain | One record for a customer (`cust_num`) on a business date (`business_dt`); the expected unique business key is (`business_dt`, `cust_num`). |
| Primary entity | Customer |



### Feature-engineering use

Use CIB to build customer-level features and to enrich event/transaction data:

- customer segment, customer type, residency/geography, tenure, lifecycle and
  status features;
- point-in-time customer profile features for a transaction or scoring date;
- customer cohorts, population filters, and entity-level train/test splits;
- static or slowly changing context added to FTR transaction features.

Avoid using values from a CIB snapshot after the prediction/scoring timestamp.
When history is available, select the latest `business_dt` that is on or before
the event date to prevent temporal leakage.
