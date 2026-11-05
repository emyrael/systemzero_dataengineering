# Folder guide

What each folder in this project is for, what you run, and what you can ignore.
The pipeline reads messy e-commerce files, checks them, computes business numbers,
draws them as a graph in SurrealDB, and writes a quality report plus a dashboard.

```text
raw files (data/)
    → Bronze          outputs/processed/bronze/        exact copy + lineage
    → Silver validated outputs/processed/silver/validated/   typed, one record at a time
    → quarantine      outputs/quarantine/             rows that cannot be trusted
    → Silver clean    outputs/processed/silver/clean/  deduped, cross-checked
    → quality report  outputs/quality_report.md
    → Gold            outputs/processed/gold/          features
    → SurrealDB       Docker, namespace test, database ecommerce
    → dashboard       outputs/dashboard/index.html
```

Run everything:

```bash
docker compose up -d surrealdb
python run_pipeline.py
```

Run one stage (it reads the previous stage’s files from disk):

```bash
python run_pipeline.py --stages features graph
```

Reference date for every “days since” and “overdue” number is **2025-01-01**.
Nothing uses the clock, so the same files produce the same tables.

---

## Root

Files that sit next to the folders, not inside one.

| File | What it does |
|---|---|
| `run_pipeline.py` | The program you start. Loads config, creates output folders, makes a run id, runs the stages you asked for. Exit code 0 when nothing failed. |
| `README.md` | The original assessment brief: the eight files, the known data problems, and the five tasks. |
| `ARCHITECTURE.md` | Why the pipeline is built this way: medallion layers, one revenue rule, what was traded off. |
| `pyproject.toml` | Project name and pytest settings (where tests live, the `integration` marker). |
| `requirements.txt` | Libraries: pandas, pydantic, pyarrow, loguru, pyyaml, phonenumbers, surrealdb, pytest. |
| `docker-compose.yml` | Starts SurrealDB 4.0.0 in Docker (and an unused Redis). Data is kept in a Docker volume. |
| `daygent.yml` | Config for an optional code-map tool. Not used by the pipeline. |
| `.gitignore` | Ignores the virtualenv, caches, logs, the embedded graph database, and the Bronze/Silver/Gold parquet files. The written reports under `outputs/` are kept. |

`src/pipeline.py` (inside `src/`, listed here because it is the stage list) runs stages in this order:

`ingest → validate → clean → report → features → graph → dashboard`

A stage that crashes does not stop the others. Stages that depend on the crashed one are skipped.

---

## `config/`

One file: `pipeline_config.yaml`. Every path, source file, and business constant lives here so the Python code does not hardcode them.

| Section | What it sets |
|---|---|
| `run` | `as_of_date: 2025-01-01` and the log level. |
| `paths` | Where raw data is read and where Bronze, Silver, Gold, quarantine, logs, the quality report, and the dashboard are written. |
| `sources` | The eight files: format (`csv`, `json`, `parquet`), the JSON key that holds the records, and the primary key. |
| `reference` | Base currency USD, payment terms (NET15 = 15 days, and so on), and which country belongs to which region. |
| `features` | Customer lifetime used for CLV (36 months) and the stock-turnover window (365 days). |
| `surrealdb` | `ws://localhost:8000/rpc`, namespace `test`, database `ecommerce`. User and password come from the environment, default `root` / `root`. |

Override the file with the environment variable `PIPELINE_CONFIG`.
Override the database URL with `SURREAL_URL`.

---

## `data/`

The raw extract. The pipeline only reads this folder. Nothing here is cleaned or rewritten.

| File | Shape | About |
|---|---|---|
| `products.csv` | ~505 rows | Catalog. Prices, stock, category, vendor. |
| `customers.json` | ~1,003 rows under the key `customers` | People, with address, preferences, and metadata nested inside. |
| `sales_transactions.parquet` | ~5,010 rows | One line per shop sale. |
| `vendors.json` | 10 rows under `vendors` | Suppliers. |
| `invoices.csv` | ~805 rows | Bills from vendors. Line items are JSON stuffed into the column `line_items_json`. |
| `support_tickets.json` | 20 rows (the file claims 250) | Ticket text. |
| `product_reviews.json` | 30 rows (claims 150) | Review text. |
| `call_transcripts.json` | 10 rows (claims 25) | Phone calls with speaker lines. |

The three “claims more than it holds” files are partial extracts. Ingest logs that and continues.

---

## `src/`

All pipeline code. Each subfolder is one stage, except `utils/` which is shared.

### `src/ingestion/`

**Bronze.** Reads each file in `config` and writes `outputs/processed/bronze/<name>.jsonl`.

`loaders.py` keeps the record exactly as it arrived (CSV values stay strings) and adds lineage:

- which run (`_batch_id`, `_ingested_at`)
- which file and its checksum (`_source_file`, `_source_file_sha256`)
- which row, the business key, and a content hash (`_source_row`, `_record_key`, `_record_hash`)

Invoice lines are pulled out of `line_items_json` into a ninth file, `invoice_lines.jsonl`, keyed by invoice id plus line number. A file that fails to load is recorded in `_manifest.json` and the other files still load.

### `src/validation/`

**One record at a time.** Reads Bronze and writes:

- good rows to `outputs/processed/silver/validated/<entity>.parquet`
- rejected rows to `outputs/quarantine/<entity>.csv`, with the rule and the raw record

| File | Role |
|---|---|
| `rules.py` | The catalog of every check: id, action (`fix`, `flag`, `quarantine`, `drop`), and a one-line description. Schemas may only raise ids that are listed here. |
| `base.py` | Shared helpers. `require` rejects the row. `soft` empties a bad field and flags the row. `standardized` records a fix when the cleaned value differs from the raw one. |
| `validator.py` | Loops Bronze, runs the matching schema, writes parquet and quarantine, writes `_validation_metrics.json`. |
| `schemas/catalog.py` | Product and vendor. |
| `schemas/customers.py` | Customer. Flattens address and preferences. Source spend is renamed `src_*` because it does not match the transactions. |
| `schemas/sales.py` | Shop transactions. Quantity 0 is rejected. A negative quantity is a return. |
| `schemas/invoices.py` | Invoice header and invoice line. Due date is recomputed from the payment terms because the source due date always equals the invoice date. |
| `schemas/support.py` | Tickets, reviews, and calls. |

A row is rejected only when the whole record cannot be trusted (no usable id, a sale of zero units). A bad price or a bad email is emptied and the rest of the row is kept.

### `src/cleaning/`

**Whole tables, across entities.** Reads Silver validated and writes `outputs/processed/silver/clean/`.

| File | Role |
|---|---|
| `standardize.py` | The parsers validation uses: null tokens, dates, booleans, currency aliases, phones (E.164), emails, names. Slash dates are day-first. Dash dates are month-first. |
| `clean.py` | Dedupes on the primary key (latest `updated_at` wins). Flags foreign keys that match nothing, without deleting the row. Flags shared emails, colliding SKUs, and reviews that claim a verified purchase with no matching sale. Relabels currency to USD when the amount matches the USD catalog. Fills a missing category from the product name when that name maps to exactly one category. |

Removed duplicates go to `outputs/quarantine/_duplicates.csv`. In this extract all 41 removed rows are exact copies.

### `src/features/`

**Gold.** `build.py` reads Silver clean and writes four parquet files under `outputs/processed/gold/`, plus `outputs/feature_samples.md`.

Revenue is one rule, used again by the graph and the dashboard: payment status `paid`, order not cancelled, merchandise after discount, tax and shipping excluded, returns negative.

| Table | One row per | Examples |
|---|---|---|
| `customer_features` | customer | orders, spend, tenure, frequency, average order value, lifetime value, days since last purchase, RFM score and segment |
| `product_features` | product | units, revenue, share, velocity, stock turnover, price tier, rating times vendor reliability |
| `vendor_features` | vendor | active products, revenue, average rating, on-time payment rate, outstanding balance |
| `invoice_features` | invoice | days to payment, days overdue, distinct products, discount versus catalog cost, reconciliation flag |

Definitions and formulas are in `docs/FEATURES.md`.

### `src/graph/`

**SurrealDB.** Reads Silver clean and Gold, builds nodes and edges in Python, loads them, runs eight queries.

| File | Role |
|---|---|
| `schema.surql` | The database shape: 10 node tables, 13 edge tables, field types. Reapplied on every load. |
| `model.py` | Turns dataframes into nodes and edges. An edge is created only when both ends exist. Skipped edges are counted. No email, phone, or street address is copied in. |
| `loader.py` | Connects, applies the schema, upserts in batches of 500, then deletes records this run did not write. Fails if the counts do not match what was built. |
| `queries.py` | The eight questions (reliable vendors, one customer’s history, related products, top customers, vendor reach, invoice costs, overdue vendors, payment versus sales). |
| `stage.py` | The pipeline entry. Writes `outputs/graph_queries.md`. |

Record ids are the business keys (`customer:⟨CUS-00000223⟩`), so a second load updates the same records instead of duplicating them. The model is explained in `docs/GRAPH_MODEL.md`.

On the assessment data the loaded graph is **2,390 nodes and 11,833 edges**.

### `src/reporting/`

| File | Role |
|---|---|
| `quality_report.py` | Reads the Bronze manifest and the validation and cleaning metrics. Overwrites `outputs/quality_report.md`. Same inputs, same file. |
| `dashboard.py` | Reads Gold (and the quality metrics). Writes one HTML file, `outputs/dashboard/index.html`, with the charts drawn inside it. Aggregates only: no customer name, id, or contact detail. |

### `src/utils/`

| File | Role |
|---|---|
| `config.py` | Loads the YAML into typed, frozen objects. Relative paths are anchored at the project root. |
| `io.py` | JSON-lines, parquet, and CSV writes. Each write goes to a temp file and is renamed, so a crash does not leave a half-written file. |
| `log.py` | A colored console line plus one JSON log per run in `outputs/logs/`. |

---

## `docs/`

Explanations. The pipeline does not read these at runtime, except that the graph report points at `GRAPH_MODEL.md`.

| File | What it answers |
|---|---|
| `CLEANING_RULES.md` | How a raw value becomes a Silver value, and why each rule fixes, flags, quarantines, or drops. |
| `FEATURES.md` | Every Gold column: the formula and the reason. |
| `GRAPH_MODEL.md` | Nodes, edges, why orphans have no edge, how a reload stays safe, Docker versus the embedded engine. |
| `LOGGING.md` | What INFO and WARNING look like, including a graph load and a database that is down. |
| `LINEAGE.md` | Which function reads and writes which file. Generated as a code map. |
| `FOLDER_GUIDE.md` | This file. |

---

## `scripts/`

`data_profile.py` is a read-only look at `data/` before the pipeline runs. It writes `outputs/profiling/raw_data_profile.md` and `.json`. It does not clean anything and no stage calls it. It is how the date rules and the “currency labels are wrong” finding were established.

---

## `tests/`

Pytest. `conftest.py` loads the real config but points every output path at a temporary directory, so a test does not overwrite `outputs/`.

| Folder | What it covers |
|---|---|
| `tests/unit/` | Parsers, schemas, the rule registry, dedup and references, feature formulas, the graph mapping, config, logging, the dashboard, the stage runner. Small fake tables, no database. |
| `tests/integration/test_pipeline_run.py` | Runs ingest through features on the real files. Checks that every record is accounted for, duplicates are the only removals, feature totals match recognised revenue, and a second run matches the first apart from the batch id. |
| `tests/integration/test_graph.py` | Loads the real graph into an in-memory SurrealDB. If Docker is up, it also loads into the server, in a database named `pytest_graph`, not `ecommerce`. |

```bash
pytest tests/unit -q
pytest tests/integration -q
```

---

## `outputs/`

Everything the pipeline writes. Safe to delete and regenerate, except you would lose the last run until you run again.

| Path | Written by | What it is |
|---|---|---|
| `processed/bronze/*.jsonl` and `_manifest.json` | ingest | Raw records plus lineage. |
| `processed/silver/validated/*.parquet` and `_validation_metrics.json` | validate | Typed rows and the fix/flag/reject counts. |
| `processed/silver/clean/*.parquet` and `_cleaning_metrics.json` | clean | The tables features and the graph read. |
| `processed/gold/*_features.parquet` | features | One feature table per entity. |
| `quarantine/<entity>.csv` | validate | Rejected records and the reason. |
| `quarantine/_duplicates.csv` | clean | The dropped copy and which row was kept. |
| `quality_report.md` | report | Counts per entity and per rule. Replaced on every successful report stage. |
| `feature_samples.md` | features | Top 5 rows of each Gold table. |
| `graph_queries.md` | graph | The eight questions, the SurrealQL, and the rows that came back. |
| `dashboard/index.html` | dashboard | One offline HTML page of KPIs. |
| `profiling/` | `scripts/data_profile.py` | The raw-data profile. Not produced by `run_pipeline.py`. |
| `logs/pipeline_<run-id>.jsonl` | every run | Structured log. Gitignored. |
| `graph/ecommerce.db/` | graph, only if `SURREAL_URL` points at the embedded engine | A local SurrealDB file. Gitignored. The Docker database is not in this folder; it is in the Docker volume. |

---

## What is not a source folder

| Path | Why it is here |
|---|---|
| `.venv/` | The Python environment. Run `.venv/bin/python` and `.venv/bin/pytest` from it. |
| `__pycache__/`, `.pytest_cache/` | Bytecode and pytest’s cache. Created when you run code or tests. |
| `.git/` | History. |

---

## Where a single fact lives

Use this when someone asks “where would I look?”

| Question | Open |
|---|---|
| What did the file contain before any fix? | `outputs/processed/bronze/<entity>.jsonl` |
| Why was this row rejected? | `outputs/quarantine/<entity>.csv` |
| What is the cleaned customer table? | `outputs/processed/silver/clean/customers.parquet` |
| What is Jennifer Wang’s lifetime value? | `outputs/processed/gold/customer_features.parquet` |
| What is in the database right now? | Docker database `test` / `ecommerce`, described by `outputs/graph_queries.md` |
| Why does this rule exist? | `docs/CLEANING_RULES.md` and `src/validation/rules.py` |
| Why is there no edge for this sale? | The customer or product id is not a node. The skip counts are at the top of `outputs/graph_queries.md`. |
