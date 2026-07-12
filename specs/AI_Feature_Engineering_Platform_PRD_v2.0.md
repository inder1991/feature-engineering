# Product Requirements Document (PRD) Addendum: Catalog Ingestion & Onboarding

## 1. Additional Product Scope
The platform shall also:
* Pull dataset inventories alongside their associated business terms, glossaries, and contextual metadata directly from OpenMetadata.
* Provide an official asset-onboarding workflow that explicitly registers discovered datasets into the local platform tenancy.
* Restrict all downstream AI feature generation, automated profiling (EDA), and data quality monitoring exclusively to successfully onboarded datasets.

---

## 2. New Functional Requirements

### FR-25: OpenMetadata Dataset Discovery & Metadata Ingestion
The platform shall pull data catalog inventories from OpenMetadata to evaluate prospective assets before formal onboarding.

#### Metadata Extraction Scope
For every discovered dataset, the platform must query the OpenMetadata API to ingest:
* **Core Technical Metadata**: Fully Qualified Name (FQN), database service type, table schemas, column data types, and primary/foreign key structures.
* **Semantic Context**: Assigned Business Glossary definitions, associated Business Terms, and functional Domain classifications.
* **Operational Attributes**: Assigned Data Owners/Stewards, data lineage origins, existing data tier tags (e.g., Tier 1 - Critical), and asset creation timestamps.

### FR-26: Formal Asset Onboarding Workflow
The platform shall provide an automated mechanism to officially onboard discovered datasets into the platform environment.

Use code with caution.[ OpenMetadata Catalog ]│▼[ FR-25: Inventory Discovery ](Pulls Tables, Terms, Owners & Lineage)│▼[ FR-26: Onboarding Trigger ] ──(Registers Tenancy & Credentials)│▼┌─────────────────┴─────────────────┐│     Onboarded Dataset State       │└─────────────────┬─────────────────┘│┌────────┴────────┐▼                 ▼[ FR-18 ]         [ FR-22 ]Automated EDA    AI-Agent Feature& Profiling        Generation
#### Onboarding Mechanics
* **Target Isolation**: Users shall browse discovered OpenMetadata catalog items and trigger an onboarding action for specific tables, schemas, or entire data products.
* **Storage Registration**: The system must register the data access credentials, storage endpoints, and target tenancy path configurations during the onboarding transaction.
* **State Management**: Onboarded datasets shall be labeled with an `ONBOARDED` state flag within the internal Knowledge Base to distinguish them from unmanaged catalog files.

### FR-27: Execution Guardrails & Scope Restriction
The platform shall strictly isolate and restrict execution layers based on onboarding status.

#### Operational Constraints
* **Profiling Isolation**: Automated Dataset Profiling (FR-18) and Interactive HTML EDA Report Generation (FR-19) shall only execute against datasets containing an active `ONBOARDED` status flag.
* **Feature Engineering Lock**: AI-Agent Feature Generation (FR-22) and semantic feature recommendation loops must reject user execution prompts targeting datasets that have not been successfully onboarded.
* **Resource Optimization**: Un-onboarded catalog inventories discovered via OpenMetadata shall strictly exist as read-only metadata definitions inside the Knowledge Base to eliminate unnecessary compute processing.
