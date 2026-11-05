# Senior Data Engineer - Take-Home Assessment

> **Reviewers:** the solution is summarised in [Solution: Where to Find Each Deliverable](#solution-where-to-find-each-deliverable),
> and the design rationale is in [`ARCHITECTURE.md`](ARCHITECTURE.md). The original brief follows.

## Overview

Welcome to the technical assessment for the **Senior Data Engineer** position. This exercise is designed to evaluate your ability to build production-quality data pipelines, handle real-world data quality challenges, perform feature engineering, and model data for a graph database (SurrealDB GraphRAG).

**Time Expectation:** 4-8 hours of focused work (you have up to 3 days to submit)

**Domain Context:** You are working with an e-commerce company that wants to leverage its data for AI-powered recommendations and analytics. The data comes from various source systems with known quality issues that reflect real-world scenarios.

---

## Scenario

Your company has acquired data from multiple sources: product catalogs, customer records, sales transactions, and vendor information. Your task is to build a data pipeline that:

1. **Ingests** raw data from multiple formats
2. **Cleans and validates** the data, handling quality issues
3. **Engineers features** for analytics and ML use cases
4. **Models and loads** the data into a SurrealDB graph database for GraphRAG applications

```mermaid
flowchart LR
    subgraph Sources
        A[products.csv]
        B[customers.json]
        C[sales_transactions.parquet]
        D[vendors.json]
        E2[invoices.csv]
        F2[support_tickets.json]
        G2[product_reviews.json]
        H2[call_transcripts.json]
    end
    
    subgraph Your Pipeline
        E[Ingestion Layer]
        F[Validation & Cleaning]
        G[Feature Engineering]
        H[Graph Modeling]
        I[SurrealDB Loader]
    end
    
    subgraph Outputs
        J[(SurrealDB GraphRAG)]
        K[Quality Reports]
        L[Processed Data]
    end
    
    A --> E
    B --> E
    C --> E
    D --> E
    E2 --> E
    F2 --> E
    G2 --> E
    H2 --> E
    E --> F
    F --> G
    G --> H
    H --> I
    I --> J
    F --> K
    G --> L
```

---

## Data Description

You are provided with eight interconnected data sources representing an e-commerce business. The data **intentionally contains various data quality issues** that reflect real-world scenarios you must identify and handle.

### Source Files

| File | Format | Description | Records |
|------|--------|-------------|---------|
| `data/products.csv` | CSV | Product catalog with attributes | ~500 |
| `data/customers.json` | JSON | Customer profiles with nested addresses | ~1,000 |
| `data/sales_transactions.parquet` | Parquet | B2C transaction history | ~5,000 |
| `data/vendors.json` | JSON | Vendor/supplier information | 10 |
| `data/invoices.csv` | CSV | B2B invoices from vendors (inventory purchases) | ~800 |
| `data/support_tickets.json` | JSON | Customer support tickets with summaries | ~20 |
| `data/product_reviews.json` | JSON | Product reviews with ratings and sentiment | ~30 |
| `data/call_transcripts.json` | JSON | Detailed phone call transcripts with speaker diarization | ~10 |

### Data Relationships

```mermaid
erDiagram
    VENDOR ||--o{ PRODUCT : supplies
    VENDOR ||--o{ INVOICE : "bills us"
    INVOICE ||--o{ INVOICE_LINE : contains
    INVOICE_LINE }o--|| PRODUCT : "for product"
    CUSTOMER ||--o{ TRANSACTION : makes
    PRODUCT ||--o{ TRANSACTION : "sold in"
    CUSTOMER }|--|| REGION : "located in"
    VENDOR }|--|| REGION : "based in"
    PRODUCT }|--|| CATEGORY : "belongs to"
    CUSTOMER ||--o{ SUPPORT_TICKET : opens
    SUPPORT_TICKET }o--o| PRODUCT : "about product"
    SUPPORT_TICKET ||--o| CALL_TRANSCRIPT : "has recording"
    CUSTOMER ||--o{ REVIEW : writes
    PRODUCT ||--o{ REVIEW : receives
    
    VENDOR {
        string vendor_id PK
        string vendor_name
        string country
        string region
        float reliability_score
        string status
    }
    
    PRODUCT {
        string product_id PK
        string vendor_id FK
        string sku
        string product_name
        string category
        float price
        int stock_quantity
    }
    
    CUSTOMER {
        string customer_id PK
        string full_name
        string email
        json address
        string segment
        float total_spend
    }
    
    TRANSACTION {
        string transaction_id PK
        string customer_id FK
        string product_id FK
        datetime transaction_date
        int quantity
        float total_amount
        string order_status
    }
    
    INVOICE {
        string invoice_id PK
        string vendor_id FK
        string invoice_number
        date invoice_date
        date due_date
        float total_amount
        string payment_status
    }
    
    INVOICE_LINE {
        int line_number
        string product_id FK
        int quantity
        float unit_cost
        float line_total
    }
    
    SUPPORT_TICKET {
        string ticket_id PK
        string customer_id FK
        string product_id FK
        string channel
        string category
        string priority
        string status
        text transcript
        int satisfaction_score
    }
    
    REVIEW {
        string review_id PK
        string product_id FK
        string customer_id FK
        int rating
        string title
        text review_text
        int helpful_votes
        boolean verified_purchase
        string sentiment
    }
    
    CALL_TRANSCRIPT {
        string call_id PK
        string ticket_id FK
        string customer_id FK
        string agent_id
        datetime call_start
        int duration_seconds
        string language
        json utterances
        string sentiment_overall
    }
```

### Data Types Covered

The datasets include a comprehensive range of data types to test your handling capabilities:

| Data Type | Examples in Dataset |
|-----------|---------------------|
| **Strings** | product_name, customer_name, vendor_name |
| **Integers** | quantity, stock_quantity, review_count |
| **Floats** | price, total_amount, reliability_score |
| **Dates** | transaction_date, registration_date |
| **Timestamps** | created_at, updated_at |
| **Booleans** | is_active, email_verified |
| **JSON/Nested** | address, contact, preferences |
| **Arrays/Lists** | categories, certifications, tags |
| **Enums** | status, segment, payment_method |
| **Nullable** | Various fields with intentional nulls |
| **UUIDs/IDs** | product_id, customer_id, transaction_id |

### Known Data Quality Issues

The data contains **intentional issues** you should identify and handle:

**Products:**
- Duplicate SKUs
- Negative prices
- Missing categories
- Inconsistent date formats
- Invalid vendor references (orphaned records)
- Inconsistent boolean representations
- Mixed case naming conventions
- Encoding issues (special characters)

**Customers:**
- Duplicate and invalid email addresses
- Inconsistent phone number formats
- Missing required fields
- Future dates (invalid registration dates)
- Inconsistent gender values
- Invalid age values
- Mixed date formats

**Sales Transactions:**
- Negative quantities (returns/refunds)
- Zero quantities
- Invalid customer references
- Invalid product references
- Duplicate transactions
- Inconsistent currency codes
- Mixed boolean representations

**Vendors:**
- Inactive vendors with products
- Missing contact information
- Pending approval status with transactions

**Invoices:**
- Duplicate invoice numbers
- Invalid vendor references
- Line items referencing non-existent products
- Calculation mismatches (totals don't add up)
- Negative quantities (credits/returns)
- Inconsistent date formats
- Payment dates before invoice dates
- Inconsistent currency codes
- Nested JSON in CSV (line_items_json column)

**Support Tickets:**
- Invalid customer references (INVALID_ID)
- Missing ticket IDs (empty strings)
- Inconsistent channel values (lowercase vs UPPERCASE)
- Inconsistent status values (mixed case)
- Invalid date formats (20-03-2025 instead of ISO)
- Satisfaction scores out of range (6 when max is 5)
- Null satisfaction scores for resolved tickets
- Null resolved_at timestamps for resolved tickets
- Missing product references for product-related tickets

**Product Reviews:**
- Invalid product references (PRD-INVALID)
- Missing review IDs (empty strings)
- Invalid ratings (negative values, null)
- Inconsistent date formats (25/03/2025 vs ISO)
- Reviews by customers who never purchased (verified_purchase mismatch)
- Missing sentiment analysis for completed reviews

**Call Transcripts:**
- Missing ticket references (null ticket_id)
- Invalid customer references (INVALID)
- Negative duration values
- Missing or incomplete utterance arrays
- Inconsistent sentiment values
- Quality scores outside valid range
- Timestamps out of order in utterances
- Missing agent information

---

## Tasks

### Task 1: Data Ingestion & Validation (Core)

Build a data ingestion pipeline that:

1. **Loads data** from all eight source files (CSV, JSON, Parquet)
2. **Validates data** against defined schemas
3. **Generates a data quality report** summarizing all issues found
4. **Handles errors gracefully** without failing the entire pipeline

```mermaid
flowchart TD
    A[Raw Data Sources] --> B{Schema Validation}
    B -->|Valid| C[Clean Records]
    B -->|Invalid| D[Quarantine Records]
    C --> E[Quality Metrics]
    D --> E
    E --> F[Quality Report]
```

**Deliverables:**
- [ ] Ingestion code supporting all five source files
- [ ] Schema definitions (use Pydantic, Pandera, or similar)
- [ ] Data quality report (markdown or HTML)
- [ ] Quarantine file with rejected records and rejection reasons

---

### Task 2: Data Cleaning & Transformation (Core)

Clean and transform the validated data:

1. **Standardize** formats (dates, phone numbers, booleans, currencies)
2. **Resolve** duplicates (define and implement deduplication strategy)
3. **Handle** missing values (imputation, defaults, or flagging)
4. **Normalize** inconsistent values (case, encoding, formats)
5. **Validate** referential integrity (fix or flag orphaned records)

**Deliverables:**
- [ ] Transformation code with clear data lineage
- [ ] Documentation of cleaning rules applied
- [ ] Statistics on records cleaned vs. rejected

---

### Task 3: Feature Engineering (Core)

Create derived features for analytics and ML:

**Customer Features:**
- `customer_lifetime_value`: Total spend adjusted for tenure
- `purchase_frequency`: Orders per month since registration
- `average_order_value`: Mean transaction amount
- `days_since_last_purchase`: Recency metric
- `customer_segment_score`: Derived from RFM analysis

**Product Features:**
- `revenue_contribution`: Total revenue from this product
- `velocity_score`: Sales frequency metric
- `stock_turnover_rate`: Stock efficiency
- `price_tier`: Low/Medium/High/Premium categorization
- `vendor_reliability_weighted_score`: Product score adjusted by vendor reliability

**Vendor Features:**
- `total_products_supplied`: Count of active products
- `revenue_generated`: Total sales from vendor's products
- `product_quality_score`: Average rating of vendor's products
- `invoice_payment_rate`: Percentage of invoices paid on time
- `average_invoice_value`: Mean invoice amount
- `total_outstanding_balance`: Sum of unpaid invoice balances

**Invoice Features:**
- `days_to_payment`: Time between invoice and payment dates
- `days_overdue`: Days past due date (for unpaid invoices)
- `line_item_diversity`: Number of unique products per invoice
- `discount_rate_achieved`: Actual discount vs. standard terms
- `reconciliation_flag`: Whether invoice matches expected product costs

**Deliverables:**
- [ ] Feature engineering code
- [ ] Feature documentation (definitions, formulas, business logic)
- [ ] Sample output showing computed features

---

### Task 4: Graph Modeling & SurrealDB Loading (Advanced)

Model the data as a graph and load it into SurrealDB for GraphRAG applications:

1. **Design** the graph schema with nodes and edges
2. **Implement** the data loading logic for SurrealDB
3. **Create** sample queries demonstrating graph traversal

```mermaid
graph TD
    subgraph Nodes
        V[Vendor]
        P[Product]
        C[Customer]
        Cat[Category]
        R[Region]
        I[Invoice]
    end
    
    subgraph Edges
        V -->|SUPPLIES| P
        V -->|BILLED_US| I
        I -->|CONTAINS| P
        P -->|BELONGS_TO| Cat
        C -->|PURCHASED| P
        C -->|LOCATED_IN| R
        V -->|BASED_IN| R
        P -->|SIMILAR_TO| P
    end
```

**Graph Schema Requirements:**

```surql
-- Example SurrealDB schema (candidates should design their own)

-- Nodes
DEFINE TABLE vendor SCHEMAFULL;
DEFINE TABLE product SCHEMAFULL;
DEFINE TABLE customer SCHEMAFULL;
DEFINE TABLE category SCHEMAFULL;
DEFINE TABLE region SCHEMAFULL;
DEFINE TABLE invoice SCHEMAFULL;

-- Edges (relationships)
DEFINE TABLE supplies SCHEMAFULL;      -- vendor -> product
DEFINE TABLE purchased SCHEMAFULL;     -- customer -> product (with transaction details)
DEFINE TABLE belongs_to SCHEMAFULL;    -- product -> category
DEFINE TABLE located_in SCHEMAFULL;    -- customer/vendor -> region
DEFINE TABLE billed SCHEMAFULL;        -- vendor -> invoice
DEFINE TABLE invoice_item SCHEMAFULL;  -- invoice -> product (line items)
```

**Sample Graph Queries to Implement:**

1. Find all products from vendors with reliability score > 90
2. Get customer purchase history with product and vendor details
3. Find related products (same category, same vendor, bought together)
4. Identify top customers by vendor (who buys most from each vendor)
5. Calculate vendor influence score (reach to customers)
6. Reconcile invoices: match invoice line items to products and verify costs
7. Find vendors with overdue invoices and their affected products
8. Calculate vendor payment performance vs. product sales correlation

**Deliverables:**
- [ ] SurrealDB schema design document
- [ ] Data loading scripts
- [ ] At least 5 graph queries with explanations
- [ ] Brief writeup on graph modeling decisions

---

### Task 5: Production Readiness (Senior Expectations)

Demonstrate senior-level engineering practices:

1. **Testing:** Unit tests for critical functions, integration test for pipeline
2. **Logging:** Structured logging throughout the pipeline
3. **Configuration:** Externalized config (no hardcoded paths/values)
4. **Documentation:** Docstrings, type hints, and architecture documentation
5. **Error Handling:** Graceful degradation, meaningful error messages
6. **Idempotency:** Pipeline can be re-run safely

**Deliverables:**
- [ ] Test suite with >70% coverage on core logic
- [ ] Configuration file (YAML/TOML)
- [ ] `ARCHITECTURE.md` explaining design decisions
- [ ] Logging output examples

---

## Project Structure

Organize your submission as follows:

```
data-engineering/
├── README.md                    # This file
├── ARCHITECTURE.md              # Your design decisions document
├── config/
│   └── pipeline_config.yaml     # Configuration
├── data/
│   ├── products.csv             # Raw product data
│   ├── customers.json           # Raw customer data
│   ├── sales_transactions.parquet  # Raw B2C transaction data
│   ├── vendors.json             # Raw vendor data
│   └── invoices.csv             # Raw B2B invoice data
├── src/
│   ├── __init__.py
│   ├── ingestion/               # Task 1: Data loading
│   ├── validation/              # Task 1: Schema validation
│   ├── cleaning/                # Task 2: Data cleaning
│   ├── features/                # Task 3: Feature engineering
│   ├── graph/                   # Task 4: Graph modeling & SurrealDB
│   └── utils/                   # Shared utilities
├── tests/
│   ├── unit/
│   └── integration/
├── outputs/
│   ├── quality_report.md
│   ├── quarantine/
│   └── processed/
├── notebooks/                   # Optional: exploration/analysis
├── scripts/
│   └── generate_ecommerce_data.py  # Data generation script
├── requirements.txt             # Dependencies
└── run_pipeline.py              # Main entry point
```

---

## Evaluation Criteria

Your submission will be evaluated across these dimensions:

```mermaid
pie title Evaluation Weights
    "Code Quality" : 20
    "Data Engineering" : 25
    "Feature Engineering" : 15
    "Graph Modeling (SurrealDB)" : 20
    "Testing & Reliability" : 10
    "Documentation" : 10
```

### Detailed Rubric

| Criteria | Excellent | Good | Needs Improvement |
|----------|-----------|------|-------------------|
| **Code Quality** | Clean, idiomatic Python; consistent style; proper abstractions | Functional with minor style issues | Disorganized; hard to follow |
| **Data Engineering** | Handles all edge cases; efficient transformations; proper validation | Covers main cases; reasonable approach | Missing validation; brittle code |
| **Feature Engineering** | Well-reasoned features; proper documentation; business value clear | Working features with basic docs | Features not well-defined |
| **Graph Modeling** | Elegant schema; efficient queries; clear rationale | Working model with basic queries | Schema doesn't leverage graph benefits |
| **Testing** | Comprehensive tests; good coverage; meaningful assertions | Core functions tested | Minimal or no tests |
| **Documentation** | Clear architecture doc; useful comments; good README | Basic documentation | Little to no documentation |

---

## Technical Requirements

- **Language:** Python 3.10+
- **Required Libraries:** 
  - pandas, polars, or similar for data processing
  - pydantic or pandera for validation
  - surrealdb (Python client)
- **Testing:** pytest
- **Optional but Recommended:**
  - duckdb for analytical queries
  - great_expectations for data quality
  - loguru for logging

---

## Getting Started

```bash
# 1. Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start SurrealDB (using Docker; pinned v4.0.0, persistent volume, healthcheck)
docker compose up -d surrealdb
#    No Docker? SURREAL_URL=surrealkv://outputs/graph/ecommerce.db uses the embedded engine.

# 4. Run the pipeline (ingest → validate → clean → report → features → graph → dashboard)
python run_pipeline.py

# 5. Run tests
pytest tests/ -v --cov=src
```

### SurrealDB Quick Reference

```bash
# Connect to SurrealDB
surreal sql --conn http://localhost:8000 --user root --pass root --ns test --db ecommerce

# Or use Python client
from surrealdb import Surreal

async with Surreal("ws://localhost:8000/rpc") as db:
    await db.signin({"user": "root", "pass": "root"})
    await db.use("test", "ecommerce")
```

---

## Solution: Where to Find Each Deliverable

Start with [`ARCHITECTURE.md`](ARCHITECTURE.md): design decisions, trade-offs and what I would
change for production. The generated reports under `outputs/` are committed, so results can be
reviewed without running anything.

```bash
docker compose up -d surrealdb     # SurrealDB 4.0 for the graph stage
python run_pipeline.py             # ingest → validate → clean → report → features → graph → dashboard
pytest --cov=src                   # 196 tests, 93% coverage
```

| Task | Deliverable | Where |
|---|---|---|
| 1 | Ingestion for all eight source files | `src/ingestion/loaders.py` (Bronze with lineage: `outputs/processed/bronze/`) |
| 1 | Schema definitions | `src/validation/schemas/` (Pydantic v2), rules in `src/validation/rules.py` |
| 1 | Data quality report | [`outputs/quality_report.md`](outputs/quality_report.md) |
| 1 | Quarantine with reasons | [`outputs/quarantine/`](outputs/quarantine/) (raw record, rule, message; duplicates in `_duplicates.csv`) |
| 2 | Transformation code with lineage | `src/cleaning/` (lineage columns carried from Bronze to Silver and quarantine) |
| 2 | Cleaning rules | [`docs/CLEANING_RULES.md`](docs/CLEANING_RULES.md) |
| 2 | Cleaned vs rejected statistics | [`outputs/quality_report.md`](outputs/quality_report.md), section 1 |
| 3 | Feature code | `src/features/build.py` |
| 3 | Feature documentation | [`docs/FEATURES.md`](docs/FEATURES.md) |
| 3 | Sample output | [`outputs/feature_samples.md`](outputs/feature_samples.md) |
| 4 | Schema design and modelling write-up | [`docs/GRAPH_MODEL.md`](docs/GRAPH_MODEL.md), `src/graph/schema.surql` |
| 4 | Loading code | `src/graph/loader.py`, `src/graph/stage.py` |
| 4 | Graph queries with explanations (8) | `src/graph/queries.py`, results in [`outputs/graph_queries.md`](outputs/graph_queries.md) |
| 5 | Tests (>70% coverage) | `tests/unit/`, `tests/integration/` |
| 5 | Configuration | [`config/pipeline_config.yaml`](config/pipeline_config.yaml) |
| 5 | Architecture | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| 5 | Logging examples | [`docs/LOGGING.md`](docs/LOGGING.md) |
| – | KPI dashboard (aggregates only; open in a browser, works offline) | [`outputs/dashboard/index.html`](outputs/dashboard/index.html) |
| – | Raw data profile (done before any rule was written) | [`outputs/profiling/raw_data_profile.md`](outputs/profiling/raw_data_profile.md) |
| – | Code map and change impact, made with my open-source tool [Daygent](https://github.com/emyrael/daygent) | [`docs/LINEAGE.md`](docs/LINEAGE.md) |

---

## Data Profiling

**Why first.** The brief lists known data-quality issues, but cleaning rules written from a list
are guesses. The raw files were profiled before any rule was written, so every rule rests on a
measured count. Profiling also found issues the brief does not mention, and showed that some
it does mention are absent from this extract.

**Run it** (standalone and read-only: it needs no pipeline stage to have run and changes nothing):

```bash
python scripts/data_profile.py              # optional: --config PATH --out-dir DIR
```

**Outputs** (deterministic: the same input files produce byte-identical reports):

- `outputs/profiling/raw_data_profile.md`: a short summary (about three pages): issues per
  dataset, all Critical and High findings with the pipeline rule that handles them, broken
  references, the evidence behind key decisions, and the business events kept
- `outputs/profiling/raw_data_profile.json`: everything: schema, column statistics, all findings
  with samples, and recommendations

Each finding is classified as a *confirmed violation*, a *suspicious anomaly* or a *legitimate
business exception* (returns, credit lines and system/supervisor speakers are kept, not
"fixed"). Each one also names the pipeline rule in `src/validation/rules.py` that handles it.
Counts are on raw records, so they can differ slightly from the pipeline's quality report,
which counts after duplicates are removed.

**How the findings shaped the pipeline:**

| Profiling evidence | Decision |
|---|---|
| Ambiguous dates checked against the ISO `metadata.created_at` copy: `nn/nn/yyyy` agrees day-first 185/185, `nn-nn-yyyy` month-first 203/203; epoch values never agree (one day early) | Format-specific date parser; customer registration taken from metadata |
| Every checkable amount equals the USD catalog value whatever the currency label (transactions 4,901/4,901, invoice lines 2,998/2,998) | Currency labels relabelled to USD; no FX conversion |
| `due_date` equals `invoice_date` on 805/805 invoices despite NET terms | `due_date` derived from terms; source kept as `src_due_date` |
| Every duplicate key is an exact copy (5 products, 3 customers, 10 transactions, 5 invoices) | Deduplicate on primary key, keep one, log the rest |
| 4% of transactions reference `CUS-INVALID-*` / `PRD-DELETED-*` | Keep and flag orphans; the graph skips the edge |
| Negative quantities (236 transactions, 67 invoice lines) are returns and credits | Classified as business events, not quarantined |
| All 387 non-null `last_purchase_date` values are after `as_of_date` | Nulled and flagged; recency recomputed from transactions |

The full rule set and the reasoning behind it are in [`docs/CLEANING_RULES.md`](docs/CLEANING_RULES.md).

---


## Questions?

If you have questions about the requirements, please email [contact email]. We're happy to clarify scope but cannot provide implementation guidance.

---

## What We're Looking For

Beyond technical skills, we're evaluating:

- **Problem-solving approach:** How do you handle ambiguity and data quality issues?
- **Communication:** Can you explain your design decisions clearly?
- **Pragmatism:** Do you make reasonable trade-offs given time constraints?
- **Attention to detail:** Do you catch the data quality issues?
- **Production mindset:** Is this code you'd be comfortable deploying?
- **Graph thinking:** Do you understand when and how to leverage graph relationships?

Good luck! We look forward to reviewing your submission.
