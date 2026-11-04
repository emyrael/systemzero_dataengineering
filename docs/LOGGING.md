# Logging

Every run writes to two places:

| Output | Audience | Format |
|---|---|---|
| Console (stderr) | the person running the pipeline | one readable line per event, structured fields as `key=value` |
| `outputs/logs/pipeline_<run_id>.jsonl` | machines: log store, dashboards, audits | one JSON object per event, every field queryable |

Every record carries the **`run_id`** (also written to Bronze as `_batch_id`, so a log line and
the rows it describes share an id) and the **`stage`**. Counts are logged as fields
(`rows=505`, `quarantined=42`), never only inside the message text, so they can be aggregated
without parsing.

Set up in `src/utils/log.py`; the level comes from `run.log_level` in the config (the JSON file
always records DEBUG and up).

## Level policy

| Level | Used for | Example |
|---|---|---|
| INFO | progress and counts per stage and source | `Validated source=transactions total=5010 valid=4968 quarantined=42` |
| WARNING | the run continues, but a person should know | source file smaller than its envelope says; graph edges skipped because one end does not exist |
| ERROR | a stage failed; dependants are skipped, the exit code is 1 | SurrealDB not reachable |

## Example: a full run (console)

Excerpt from `python run_pipeline.py` on the assessment data (absolute paths shortened; the
`dashboard` line and the final summary come from a later run, after that stage was added):

```text
21:47:17 | INFO    | pipeline   | Pipeline started as_of_date=2025-01-01 log_file=outputs/logs/pipeline_20261104T204717Z-061308.jsonl
21:47:18 | INFO    | ingest     | Loaded source source=customers rows=1003 expected_rows=1003
21:47:18 | INFO    | ingest     | Exploded invoice lines source=invoices rows=3618
21:47:18 | INFO    | ingest     | Loaded source source=support_tickets rows=20 expected_rows=250
21:47:18 | WARNING | ingest     | Completeness: envelope total_count=250 but file holds 20 records source=support_tickets
21:47:18 | INFO    | ingest     | Stage finished duration_s=0.87
21:47:18 | INFO    | validate   | Validated source=transactions total=5010 valid=4968 quarantined=42 flagged=195
21:47:18 | INFO    | validate   | Validated source=invoices total=805 valid=805 quarantined=0 flagged=556
21:47:18 | INFO    | validate   | Stage finished duration_s=0.37
21:47:18 | INFO    | clean      | Cleaned source=transactions rows_in=4968 removed=10 rows_out=4958 fixes=70 flags=198
21:47:18 | INFO    | clean      | Cleaned source=invoice_lines rows_in=3618 removed=18 rows_out=3600 fixes=0 flags=118
21:47:18 | INFO    | clean      | Stage finished duration_s=0.14
21:47:18 | INFO    | report     | Quality report written path=outputs/quality_report.md
21:47:18 | INFO    | features   | Features written source=customer_features rows=1000 columns=14
21:47:19 | INFO    | features   | Stage finished duration_s=0.08
21:47:22 | INFO    | graph      | Graph loaded nodes=2390 edges=11833
21:47:22 | WARNING | graph      | Edges skipped: end not in graph reason=purchased:in=customer count=88
21:47:22 | WARNING | graph      | Edges skipped: end not in graph reason=invoice_item:out=product count=537
21:47:22 | INFO    | graph      | Graph queries written queries=8
21:47:22 | INFO    | graph      | Stage finished duration_s=3.63
21:47:22 | INFO    | dashboard  | Dashboard written path=outputs/dashboard/index.html net_revenue=2739162.03 orders=1102
21:47:22 | INFO    | pipeline   | Pipeline finished succeeded=['ingest', 'validate', 'clean', 'report', 'features', 'graph', 'dashboard'] failed=[] skipped=[]
```

What a reader gets from this without opening any file:
- the three text sources are partial extracts (20 of 250 tickets);
- 42 transactions were rejected, and how many records each stage fixed, flagged or removed;
- 88 purchases point at customers that do not exist, so they have no edge in the graph;
- where the time goes (the graph load).

## Example: the same event as JSON

One line of the `.jsonl` file, pretty-printed and trimmed to the useful fields:

```json
{
  "text": "2026-11-04 21:47:18.683 | INFO | src.validation.validator:run_validation:180 - Validated",
  "record": {
    "time": {"repr": "2026-11-04 21:47:18.683532+01:00", "timestamp": 1793825238.683532},
    "level": {"name": "INFO", "no": 20},
    "message": "Validated",
    "extra": {
      "run_id": "20261104T204717Z-061308",
      "stage": "validate",
      "source": "transactions",
      "total": 5010,
      "valid": 4968,
      "quarantined": 42,
      "flagged": 195
    },
    "module": "validator",
    "function": "run_validation",
    "line": 180,
    "exception": null
  }
}
```

## Example: a failure

The graph stage pointed at a server that is not running
(`SURREAL_URL=ws://localhost:8001/rpc python run_pipeline.py --stages features graph`).
The features stage still succeeds; the graph stage fails with a message that says what to do;
the process exits with code 1.

```text
21:47:35 | INFO    | features   | Stage finished duration_s=0.66
21:47:36 | ERROR   | graph      | Stage failed
OSError: Multiple exceptions: [Errno 61] Connect call failed ('::1', 8001, 0, 0), [Errno 61] Connect call failed ('127.0.0.1', 8001)
ConnectionError: SurrealDB is not reachable at ws://localhost:8001/rpc. Start it with `docker compose up -d surrealdb`, or set SURREAL_URL=surrealkv://outputs/graph/ecommerce.db to use the embedded engine.
21:47:36 | INFO    | pipeline   | Pipeline finished succeeded=['features'] failed=['graph'] skipped=[]
```

In the JSON file the error record keeps the exception type and message as fields, and the full
traceback in its `text` field:

```json
{
  "message": "Stage failed",
  "level": {"name": "ERROR", "no": 40},
  "extra": {"run_id": "20261104T204735Z-bad688", "stage": "graph"},
  "exception": {
    "type": "ConnectionError",
    "value": "SurrealDB is not reachable at ws://localhost:8001/rpc. Start it with `docker compose up -d surrealdb`, or set SURREAL_URL=surrealkv://outputs/graph/ecommerce.db to use the embedded engine.",
    "traceback": true
  }
}
```

## Querying the logs

Because counts are fields, run statistics come straight out of the file:

```bash
jq -c 'select(.record.message=="Validated") | .record.extra | {source, total, quarantined, flagged}' outputs/logs/pipeline_<run_id>.jsonl
```
```text
{"source":"products","total":505,"quarantined":0,"flagged":49}
{"source":"customers","total":1003,"quarantined":0,"flagged":431}
{"source":"transactions","total":5010,"quarantined":42,"flagged":195}
```

```bash
jq -r 'select(.record.message=="Stage finished") | "\(.record.extra.stage)\t\(.record.extra.duration_s)"' outputs/logs/pipeline_<run_id>.jsonl
```

In production these files would be shipped to the log platform, and the same fields turned into
metrics with alerts: quarantine rate per source, skipped edges, stage duration.
