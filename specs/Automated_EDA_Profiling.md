## Product Requirement Document (PRD) Addendum: Dropdown-Driven Managed Connection EDA Engine
------------------------------
## 1. Feature Overview
To prevent user manual entry errors and secure database architecture details, the system must present connection options via a strict user interface dropdown element. The agentic tool will interact directly with this dropdown vector, fetching the active user selection and the designated database table name to automatically run an in-database or memory-mapped exploratory scan.
The resulting output is a standalone HTML summary page with an explicit built-in PDF download button.

┌────────────────────────────────────────────────────────┐
│  Select Source Connection:                             │
│  ┌──────────────────────────────────────────────────┐  │
│  │ 🗄️ AWS_Redshift_Production                    │▼ │  │
│  └──────────────────────────────────────────────────┘  │
│    • Snowflake_Data_Warehouse                          │  │
│    • PostgreSQL_Analytics_Replica                      │  │
│    • Google_BigQuery_Lakehouse                         │  │
└────────────────────────────────────────────────────────┘

------------------------------
## 2. Core Functional Requirements## Requirement 1: UI Dropdown Component

* UI Element: A dynamic interface dropdown populated solely by backend profiles managed inside the platform's secure credential vault.
* Component Parameters: The user selects a friendly nickname (e.g., Snowflake_Data_Warehouse) and types the target table_name or database view string into a separate query input block.
* Security & Governance: Password strings, hosting environments, and credential pairs must be completely masked from the front-end UI layer and the LLM context workspace.

## Requirement 2: Automated EDA Scan & HTML Output

* Processing: When the user selects a profile from the dropdown and clicks "Run," the system pulls credentials securely from the backend pool and runs optimized analytical aggregation queries against the dataset.
* Output Format: The module compiles metrics directly into a single, standalone interactive HTML page bundle saved to an execution storage bucket.
* Visual Widgets Included: Interactive histograms, cross-variable correlation maps, data health telemetry status tiles, and missing value concentration charts.

## Requirement 3: Client-Side "Download as PDF"

* Action Button: A fixed header action element labeled "Download Report as PDF" must sit prominently in the top right control banner of the generated HTML report.
* Export Flow: Clicking this button invokes standard client-side browser print layout routines, prompting an immediate vector PDF file export wrapper.
* Layout Optimization: Print-focused media rules must clean the document view by dropping web utility buttons or sidebars and applying clean page breaks between major distribution charts.

------------------------------
## 3. UI Layout & Application Metadata Contract
The structural sample layout below details how the dropdown component interfaces with the data reporting engine.
## Front-End Interface Design Layout

┌────────────────────────────────────────────────────────────────────────┐
│ 🛠️ FEATURE ENGINEER TOOL: INTERACTIVE STORAGE DISCOVERY               │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│ 🗄️ Select Connection Profile: [ ❄️ Snowflake_Data_Warehouse          ▼ ]│
│ 📋 Target Table / View Name:  [ core_analytics.user_demographics      ] │
│                                                                        │
│                                                [ 🚀 Execute EDA Scan ] │
├────────────────────────────────────────────────────────────────────────┤
│ 📊 DATABASE PROFILING REPORT PANEL                 [ 💾 Download PDF ] │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│ • Database Engine : Snowflake Cloud    • Calculated Rows : 8,500,420   │
│ • Field Inventory : 14 Metrics         • Total Columns   : 14 Columns  │
│                                                                        │
│ 🚨 DATA INTEGRITY ALERT LOG                                            │
│ ───► [Registration_Date] contains 4.2% Missing cells.                  │
│ ───► [Account_Balance] is heavily right-skewed.                        │
│                                                                        │
│ 📈 CO-DEPENDENCY CORRELATION ANALYSIS (In-Database Computation)        │
│                    Account_Balance   Activity_Score   Age              │
│   Account_Balance     [ 1.00 ]          [ 0.65 ]     [ 0.22 ]          │
│   Activity_Score      [ 0.65 ]          [ 1.00 ]     [ 0.04 ]          │
│   Age                 [ 0.22 ]          [ 0.04 ]     [ 1.00 ]          │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘

## Integration Interface Configuration Contract
The application uses this parameter definition schema to map the drop-down tool selection framework safely without revealing internal parameters:

ComponentID: "COMP-UI-DROPDOWN-EDA"FrontEndInputControls:
  DropdownSourceElement: "Managed Database Connection Registry Vector"
  DefaultValueSelected: "First item in available credential pool arrays"
  TextInputElement: "Table Path Target Identifier String"ReportDeliverySpecification:
  TargetOutputFormat: "Single Self-Contained Interactive HTML Page File"
  ClientSideExportFeatures: ["Download to Local Vector PDF Document Format"]
  PrintLayoutRules:
    PageBreakStrategy: "Clean grid split alignment on major data section panels"
    InteractiveExclusions: "Strip drop-down pick lists from final printed forms"
