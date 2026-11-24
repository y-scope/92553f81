# CLP metrics benchmarks

Download public infrastructure metrics and vehicle CAN signals, compress them with CLP, and run validated SQL queries through DuckDB catalogs.

## Benchmark summary

Measured on November 23, 2026, using the full prepared datasets: 1,640,857 Westermo samples and 27,400,285 public CAN frames.

| Dataset | JSONL input | CLP size (compression ratio) | Compression time | Peak compression RSS |
|---|---:|---:|---:|---:|
| Westermo infrastructure metrics | 994.3MiB | 32.3MiB (30.8×) | 6.2s | 66.0MiB |
| Public CAN signals | 3,600.9MiB | 49.8MiB (72.2×) | 28.6s | 87.4MiB |

**Query performance:** 16 of 18 queries have P50 latency between **0.8ms and 2.1ms**. The other two compute a memory ratio across all samples (**59.9ms**) and retrieve the first 20 CAN frames in source/time/speed order (**194.4ms**). All query results were checked against the source JSONL.

Compression ratios compare total JSONL bytes with total archive bytes. P50 is the median query latency across seven measured passes after two untimed passes. Query timing excludes startup and catalog setup. See the [full benchmark report](docs/metrics-benchmark.md) for dataset coverage, measurement details, all query timings and exact SQL.

## Run

**Evaluation period: two weeks. Contact YScope for updated binaries when they expire.**

Use Linux x86_64 with Python 3.11 or 3.12, virtual-environment support and GNU `time` installed. Run from this repository's root:

These instructions use the **official DuckDB Python client**, which includes the DuckDB engine. The requirements file installs **`duckdb==1.5.6`**, the exact version required by the bundled CLP extension, along with the dataset preparation dependencies. No separate DuckDB CLI or database server is needed.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements-metrics.txt
.venv/bin/python -c "import duckdb; print(duckdb.__version__)"  # Expected: 1.5.6
.venv/bin/python scripts/benchmark_metrics.py
```

If you only want to query your own CLP archives, create the virtual environment as above and install just the matching client with `.venv/bin/python -m pip install duckdb==1.5.6`.

The runner downloads and prepares both datasets, reports compression ratio, speed and peak memory, then measures query latency using catalogs with typed views. DuckDB startup, extension loading, catalog setup and warm-up are outside query timing.

Results are saved under `.work/metrics/runs/<run>/report.md`, alongside the SQL, reference results and individual measurements. Downloads and prepared inputs are verified and reused on later runs. Each run creates fresh archives and a fresh results directory.

See [the dataset and benchmark guide](docs/metrics-datasets.md) for disk requirements, individual datasets, settings, query coverage and output files.

## Query the data

After the benchmark finishes, you can query its archives directly through the catalog it created. Choose the dataset and replace `<run>` with the results directory printed by the runner:

| Dataset | Catalog to open | Typed view | Default document table |
|---|---|---|---|
| Westermo | `.work/metrics/runs/<run>/westermo/catalog.duckdb` | `westermo.metrics` | `westermo.events` |
| Public CAN | `.work/metrics/runs/<run>/can/catalog.duckdb` | `can.signals` | `can.events` |

Start the Python client from the repository root:

```bash
.venv/bin/python
```

Open the Westermo catalog and load the extension:

```python
import duckdb

db = duckdb.connect(
    '.work/metrics/runs/<run>/westermo/catalog.duckdb',
    config={'allow_unsigned_extensions': True},
)
db.execute("LOAD 'bin/x86_64/clp_s.duckdb_extension'")
db.sql('SELECT count(*) AS samples FROM westermo.metrics').show()
```

Execute the SQL examples below in this session with `db.sql("""SQL goes here""").show()`. Use `db.execute(sql).fetchall()` when your application needs the results as Python rows. Keep this connection open while exploring the data, then call `db.close()` when finished.

### How to read a query

Queries use DuckDB SQL. CLP supplies the tables and functions that read the archives. For example, this query returns up to 20 high-load samples from one system, ordered by elapsed time:

```sql
SELECT system_id AS system, elapsed_s, load_1m AS load
FROM westermo.metrics
WHERE system_id = 'system-1' AND load_1m >= 2
ORDER BY elapsed_s
LIMIT 20;
```

| Syntax | Meaning in this query |
|---|---|
| `FROM westermo.metrics` | Read the `metrics` view in the `westermo` catalog. It covers that catalog's registered archives. |
| `SELECT system_id, elapsed_s, load_1m` | Choose the columns returned. This is **projection**. |
| `AS system`, `AS load` | Rename columns in the result. The stored field names do not change. |
| `WHERE ...` | Keep only records satisfying the condition. `AND` requires both conditions. |
| `ORDER BY elapsed_s` | Sort by elapsed time, ascending by default. Add `DESC` for descending order. |
| `LIMIT 20` | Return at most 20 rows. Without `ORDER BY`, which matching rows are returned is unspecified. |

Use single quotes for string values, such as `'system-1'`. Double quotes identify a column or field name containing spaces or punctuation, such as `_events."load-1m"`. Unquoted names such as `load_1m` need neither. A semicolon ends a SQL statement.

### Inspect documents and available fields

CLP stores documents whose fields can vary between records. Each catalog's default `events` table exposes the full document as an `_events` VARIANT, preserving its nested structure:

```sql
SELECT _events
FROM westermo.events
LIMIT 3;
```

Inspect the available document paths and source types before choosing fields to query:

```sql
SELECT DISTINCT unnest(fields, recursive := true)
FROM clp_dataset_schemas('westermo')
ORDER BY ALL;
```

The result has `field_path` and `source_type` columns. `clp_dataset_schemas('westermo')` returns schema information for that catalog. `unnest(fields, recursive := true)` expands its field lists into rows, `DISTINCT` removes duplicates and `ORDER BY ALL` sorts by the returned columns. The `:=` syntax passes a named function argument, here `recursive`. A field can appear with more than one type. To inspect the nested schema structure instead, use:

```sql
SELECT schema_tree
FROM clp_dataset_schemas('westermo');
```

You can project and filter individual document fields without creating another view. Apply the SQL type to the leaf you need:

```sql
SELECT _events.system_id::VARCHAR AS system_id,
       _events.load_1m::DOUBLE AS load
FROM westermo.events
WHERE _events.load_1m::DOUBLE >= 2
LIMIT 20;
```

Here, `_events.load_1m` follows a path inside the document. Without a type, it remains a VARIANT that can hold different kinds of values. `::DOUBLE` requests a typed floating-point leaf, and `AS load` names the result column. For a nested document such as `{"sensor":{"value":21.5}}`, the equivalent expression is `_events.sensor.value::DOUBLE`.

On supported CLP leaf accesses, the requested type selects compatible source values. Missing fields, nulls and incompatible values return SQL NULL. For example, `::DOUBLE` accepts floating-point values, while `::BIGINT` accepts signed 64-bit integers; it does not automatically turn the string `"21.5"` into a number. Use the schema inspection to choose a matching type. `TRY_CAST` is an explicit request for conversion when your data actually requires it.

### Filter, project and aggregate using typed columns

The benchmark already creates typed views. Each column projects a leaf of the document with a specified SQL type. **Use these columns for filtering, projection and aggregation** to avoid constructing full documents and to let CLP apply supported operations during scanning.

Inspect the view's columns:

```sql
DESCRIBE westermo.metrics;
```

Select just the fields you need and filter samples by load:

```sql
SELECT system_id, elapsed_s, load_1m
FROM westermo.metrics
WHERE load_1m >= 2
LIMIT 20;
```

Combine predicates in `WHERE` to narrow the records:

| Predicate | Meaning |
|---|---|
| `load_1m >= 2` | Numeric comparison. Other operators include `=`, `<>`, `<`, `<=` and `>`. |
| `system_id IN ('system-1', 'system-2')` | Match either of these exact string values. |
| `system_id LIKE 'system-%'` | Match a string pattern; `%` matches any sequence and `_` matches one character. |
| `load_1m IS NOT NULL` | Require a usable, non-null value. Use `IS NULL` to find nulls, rather than `= NULL`. |
| `load_1m >= 2 AND (system_id = 'system-1' OR system_id = 'system-2')` | Combine conditions, using parentheses to make the intended grouping explicit. |

Compute statistics over a time window for one system. Westermo's `elapsed_s` is relative seconds, so compare it with numeric bounds:

```sql
SELECT count(*) AS samples,
       avg(load_1m) AS average_load,
       min(load_1m) AS minimum_load,
       max(load_1m) AS maximum_load
FROM westermo.metrics
WHERE system_id = 'system-1'
  AND elapsed_s >= 0 AND elapsed_s < 3600;
```

`WHERE` selects the input records before the aggregate functions run. `count(*)` counts those records, while `count(load_1m)` counts only their non-null load values. `sum`, `avg`, `min` and `max` also ignore NULL values. A query with aggregates and no `GROUP BY` returns one summary row. The time bounds above include 0 and exclude 3600, so adjacent windows do not overlap.

Find the five systems with the highest average load:

```sql
SELECT system_id, count(*) AS samples, avg(load_1m) AS average_load
FROM westermo.metrics
GROUP BY system_id
ORDER BY average_load DESC
LIMIT 5;
```

`GROUP BY system_id` produces one summary row per system. The aggregate functions run separately for each group. `ORDER BY average_load DESC LIMIT 5` ranks those summaries and returns the top five. To filter the summaries themselves, add a `HAVING` condition after `GROUP BY`, such as `HAVING count(*) >= 100`.

For public CAN, open the CAN catalog from the table above and load the same extension. For example, compute wheel-speed statistics per source segment:

```sql
SELECT source_file, count(WHEEL_SPEED_FR) AS samples,
       avg(WHEEL_SPEED_FR) AS average_speed_kmh,
       max(WHEEL_SPEED_FR) AS maximum_speed_kmh
FROM can.signals
WHERE WHEEL_SPEED_FR IS NOT NULL
GROUP BY source_file
ORDER BY average_speed_kmh DESC
LIMIT 5;
```

CAN's `ts` is relative milliseconds. Restrict time-window queries to a `source_file` so unrelated device clocks are not mixed. The [query appendix](docs/metrics-benchmark.md#appendix) contains all benchmark SQL and selected results. The next section shows how to register your own archives and define your own typed view.

### Group log messages by string shape

For a catalog containing a `message` field, `_shape.message` returns the stored string shape. For example, `Error: Exit code 1` and `Error: Exit code 128` both become `Error: Exit code %int%`:

```sql
CALL clp_create_catalog('logs');
CALL clp_register('logs', '/path/to/log/archives');

SELECT _shape.message::VARCHAR AS shape, count(*) AS occurrences
FROM logs.events
GROUP BY shape
ORDER BY occurrences DESC;
```

`%int%`, `%float%` and `%str%` represent variables. `_shape_all.message` also derives shapes for text stored whole. Both projections work on the default `events` table and typed views, and their paths refer to the original document fields. Add `WHERE` conditions on `_events` fields or typed columns to analyze a subset of records.

Shape grouping with `count(*)`, `SELECT DISTINCT`, and `COUNT(DISTINCT ...)` can use stored summaries instead of scanning events. With timestamp or numeric filters, CLP checks each archive independently: fully matching archives use their summaries, nonmatching archives are skipped, and partially matching or uncertain archives evaluate the filter on their records. This optimization is automatic for both `_shape` and `_shape_all` when the archive contains the required summaries.

Shapes are available for CLP archives. `SELECT *` does not include them. Select a field such as `_shape.message`, rather than the entire `_shape` root.

## Evaluate your own dataset

Use the environment installed above. The following example takes your JSONL input through compression, catalog registration and query timing. Replace the field names and queries with ones relevant to your workload.

### 1. Export JSONL and identify the timestamp

Export one JSON object per line to `my-data.jsonl`, without a surrounding array. Keep numeric values as numbers and preserve the fields needed by your queries. For example:

```jsonl
{"ts":"2026-11-23T10:00:00.123Z","device":"sensor-1","value":21.5}
{"ts":"2026-11-23T10:00:00.456Z","device":"sensor-2","value":24.0}
```

These two records illustrate the format. Use a representative dataset for compression and query measurements.

Note the timestamp's document path, representation and precision before compressing:

| Input timestamp | Declaration |
|---|---|
| ISO date/time text with millisecond precision, as above | `--timestamp-key 'ts:precision=ms'` |
| Integer Unix epoch milliseconds, such as `1700000000123` | `--timestamp-key 'ts:precision=ms'` |
| Integer Unix epoch seconds, microseconds or nanoseconds | Use `precision=s`, `precision=us` or `precision=ns`, respectively |
| Precision varies or is unknown | `--timestamp-key 'ts:precision=auto'` infers s/ms/us/ns from numeric magnitude or text fractional digits |
| Device-relative clock, duration or counter | Omit `--timestamp-key`; query the original number and unit |

Use dotted paths for nested fields, such as `event.ts`, and repeat `--timestamp-key` for multiple timestamp fields. For numeric inputs, precision declares the input unit; it does not convert a millisecond number into nanoseconds. For text inputs, CLP parses the date and timezone, then stores the selected precision. Choosing a coarser precision discards finer digits by default. Use the known source precision, or `auto` when appropriate, to preserve timestamp precision.

### 2. Compress and record size, time and memory

Use a new output directory for each evaluation:

```bash
mkdir -p .work/my-data
/usr/bin/time -v -o .work/my-data/compression-resources.txt \
  bin/x86_64/clp-s c --single-file-archive --print-archive-stats \
  --timestamp-key 'ts:precision=ms' \
  .work/my-data/archives my-data.jsonl \
  > .work/my-data/archive-stats.jsonl
```

`compression-resources.txt` records elapsed time and peak RSS. `archive-stats.jsonl` records each archive's size and input bytes. Compression ratio is **total input JSONL bytes / total archive bytes**. Compute it from the totals, rather than averaging individual file ratios. To compress several inputs together, append their paths to the command or use `--files-from` with one input path per line.

### 3. Register a catalog and measure your queries

The example below prints the compression ratio, inspects the stored fields, creates a typed view and times three queries. Edit the mappings and predicates to match your data, especially the time window.

The three `CALL` statements set up access to your archives:

| Statement | Effect |
|---|---|
| `CALL clp_create_catalog('custom')` | Create the `custom` catalog and its default `events` table. |
| `CALL clp_register('custom', '.work/my-data/archives')` | Add the archives in that directory, making their documents queryable through `custom.events`. |
| `CALL clp_create_mapped_view('custom', 'metrics', columns := [...])` | Expose selected document leaves as typed columns in `custom.metrics`. |

`columns := [...]` passes a list of mappings to the function. Each mapping has three keys: `column_name` is the name used in SQL, `document_path` locates the source leaf, and `sql_type` specifies its type. For example, `{'column_name':'event_time', 'document_path':'ts', 'sql_type':'TIMESTAMP_NS'}` exposes the document's `ts` field as the SQL column `event_time`. A nested field would use a path such as `sensor.value`.

**Use typed leaf columns for filtering, projection and aggregation.** Match source types: `BIGINT` for integers, `DOUBLE` for floating-point values, `VARCHAR` for strings and `TIMESTAMP_NS` for timestamps declared during compression. Missing or incompatible values become NULL. A relative clock should use `BIGINT` and numeric bounds in its original unit.

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import statistics
import time
import duckdb

work = Path('.work/my-data').resolve()
input_bytes = Path('my-data.jsonl').stat().st_size
archive_bytes = sum(path.stat().st_size for path in (work / 'archives').iterdir() if path.is_file())
print(f'Input: {input_bytes:,} bytes; archives: {archive_bytes:,} bytes; ratio: {input_bytes/archive_bytes:.2f}x')

with duckdb.connect(str(work / 'catalog.duckdb'), config={'allow_unsigned_extensions': True}) as db:
    db.execute("LOAD 'bin/x86_64/clp_s.duckdb_extension'")
    db.execute("CALL clp_create_catalog('custom')")
    db.execute("CALL clp_register('custom', ?)", [str(work / 'archives')])
    fields = db.execute("SELECT DISTINCT unnest(fields, recursive := true) FROM clp_dataset_schemas('custom') ORDER BY ALL").fetchall()
    print('Fields:', fields)
    db.execute("""
        CALL clp_create_mapped_view('custom', 'metrics', columns := [
          {'column_name':'event_time', 'document_path':'ts', 'sql_type':'TIMESTAMP_NS'},
          {'column_name':'device', 'document_path':'device', 'sql_type':'VARCHAR'},
          {'column_name':'value', 'document_path':'value', 'sql_type':'DOUBLE'}
        ])
    """)
    queries = {
        'count': 'SELECT count(*) FROM custom.metrics',
        'window_stats': """
            SELECT count(value), avg(value), min(value), max(value)
            FROM custom.metrics
            WHERE event_time >= TIMESTAMP_NS '2026-11-23 10:00:00'
              AND event_time < TIMESTAMP_NS '2026-11-23 10:01:00'
        """,
        'filtered_rows': 'SELECT event_time, device, value FROM custom.metrics WHERE value > 22.0 LIMIT 20',
    }
    for _ in range(2):
        for sql in queries.values():
            db.execute(sql).fetchall()

    samples = {name: [] for name in queries}
    results = {}
    names = list(queries)
    for repetition in range(7):
        offset = repetition % len(names)
        for name in names[offset:] + names[:offset]:
            start = time.perf_counter_ns()
            rows = db.execute(queries[name]).fetchall()
            samples[name].append((time.perf_counter_ns() - start) / 1e6)
            results[name] = rows
    for name, values in samples.items():
        p95 = statistics.quantiles(values, n=100, method='inclusive')[94]
        print(f'{name}: average={statistics.mean(values):.2f}ms, P50={statistics.median(values):.2f}ms, P95={p95:.2f}ms')
        print('Result:', results[name])
PY
```

The catalog also exposes `custom.events`, whose `_events` VARIANT contains each full document. The typed `custom.metrics` view gives your benchmark ordinary SQL columns without constructing full documents for every query.

For the declared timestamp, `TIMESTAMP_NS '2026-11-23 10:00:00'` is a typed date/time literal representing a UTC instant. Compare it directly with `event_time`, even though the source was stored at millisecond precision. CLP accounts for the stored precision. For ordinary numeric clocks, compare numbers in the original unit instead.

The code runs two untimed passes, then measures planning, execution and complete result retrieval over seven passes. Startup, extension loading and registration are excluded. P50 is the median; P95 from seven samples is only a rough indicator. Check the printed results against your original data before drawing performance conclusions. With the sample above, the count is 2 and the time-window statistics are `(2, 22.75, 21.5, 24.0)`.

For a fair comparison, use the same records, predicates and returned fields in every system. Keep counts, small aggregates and full-record exports as separate queries. Record the dataset size, timestamp declaration, binary versions, machine and query results alongside the timings.
