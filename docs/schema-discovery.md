# Discover fields and schemas

CLP records are documents. Each document can have nested fields, and the fields and their types can vary between records. Schema discovery shows you that structure before you write a query.

This walkthrough answers three questions: **Which fields exist? What is the structure of the events I care about? How do I query those fields?**

The example catalog, `discovery_demo`, contains four request events: one successful payment request, two failed payment requests and one failed checkout request. Failed requests have a nested `error` object. If you want to run the examples, [prepare this catalog](#prepare-the-example) first. For your own data, substitute your catalog name.

## 1. Find the available fields

```sql
SELECT DISTINCT unnest(fields, recursive := true)
FROM clp_dataset_schemas('discovery_demo')
ORDER BY ALL;
```

The result lists each document path and its type:

| field_path | source_type |
|---|---|
| error | object |
| error.code | string |
| error.retryable | boolean |
| latency_ms | float |
| message | string |
| service | string |
| status | integer |
| ts | timestamp |

`error.code` means the `code` field inside the `error` object. A path can appear with several types if records store different kinds of values there. This list combines the fields across the catalog, so it does not imply that every record contains every field.

## 2. Inspect the schema of matching events

Suppose you want to investigate failed payment requests. Put that condition in `predicate` to discover the schema of just those events:

```sql
SELECT schema_tree, matching_rows
FROM clp_dataset_schemas('discovery_demo', predicate := $$
    _events.service::VARCHAR = 'payments'
    AND _events.status::BIGINT >= 500
$$);
```

Both failed payment requests have the same structure, so the result contains one schema with `matching_rows = 2`. Its `error` branch is shown here, formatted for readability:

```json
{
  "types": ["object"],
  "children": {
    "code": {"types": ["string"], "children": {}},
    "retryable": {"types": ["boolean"], "children": {}}
  }
}
```

The tree shows that `error` contains a string `code` and a boolean `retryable`. Each node has a `types` list and a `children` object. `schema_tree` is a VARIANT, so you can select a branch with `schema_tree.children.error`.

Write the predicate as you would write a `WHERE` condition on the default `events` table. `_events` is the document, and `::VARCHAR` or `::BIGINT` specifies the type of the field being tested. The `$$...$$` quotes the condition while allowing single quotes around values such as `'payments'`.

**Put event conditions in `predicate`.** An outer `WHERE` filters the returned schema rows. If matching events have different structures, discovery returns a row for each schema.

## 3. Query the fields you found

The default table `discovery_demo.events` gives you access to the actual documents. Use the same condition and select the fields you need:

```sql
SELECT _events.error.code::VARCHAR AS error_code,
       _events.latency_ms::DOUBLE AS latency_ms
FROM discovery_demo.events
WHERE _events.service::VARCHAR = 'payments'
  AND _events.status::BIGINT >= 500
ORDER BY latency_ms;
```

| error_code | latency_ms |
|---|---:|
| timeout | 400.5 |
| timeout | 450.5 |

**For best performance, filter, project and aggregate typed leaf fields**, such as `_events.error.code::VARCHAR`. A leaf is a value at the end of a document path. For repeated analysis, a [typed view](../README.md#filter-project-and-aggregate-using-typed-columns) exposes these paths as ordinary SQL columns.

## Reference

### Prepare the example

Follow the [environment setup](../README.md#run) first. Run these commands from the repository root, using a new output directory:

```bash
mkdir -p .work/discovery
cat > .work/discovery/events.jsonl <<'JSONL'
{"ts":"2026-11-24T10:00:00.123Z","service":"payments","status":200,"latency_ms":12.5,"message":"Request 10 completed"}
{"ts":"2026-11-24T10:00:01.456Z","service":"payments","status":503,"latency_ms":400.5,"message":"Request 11 failed","error":{"code":"timeout","retryable":true}}
{"ts":"2026-11-24T10:00:02.789Z","service":"checkout","status":502,"latency_ms":300.5,"message":"Request 12 failed","error":{"code":"upstream"}}
{"ts":"2026-11-24T10:00:03.012Z","service":"payments","status":503,"latency_ms":450.5,"message":"Request 13 failed","error":{"code":"timeout","retryable":true}}
JSONL
bin/x86_64/clp-s c --single-file-archive \
  --timestamp-key 'ts:precision=ms' \
  .work/discovery/archives .work/discovery/events.jsonl
.venv/bin/python
```

In the official DuckDB Python client, load the extension and register the archives:

```python
import duckdb

db = duckdb.connect(config={'allow_unsigned_extensions': True})
db.execute("LOAD 'bin/x86_64/clp_s.duckdb_extension'")
db.execute("CALL clp_create_catalog('discovery_demo')")
db.execute("CALL clp_register('discovery_demo', '.work/discovery/archives')")
```

Run SQL from this guide with `db.execute("""SQL goes here""").fetchall()`. For example:

```python
db.execute("""
    SELECT count(*) FROM discovery_demo.events
""").fetchall()
```

This returns `[(4,)]`. Keep the connection open while following the walkthrough. To format a tree branch as JSON in Python, use:

```python
import json

error_schema = db.execute("""
    SELECT schema_tree.children.error
    FROM clp_dataset_schemas('discovery_demo', predicate := $$
        _events.service::VARCHAR = 'payments'
        AND _events.status::BIGINT >= 500
    $$)
""").fetchone()[0]
print(json.dumps(error_schema, indent=2))
```

### Find when a field first or last appears

Use `clp_first_seen(field, timestamp)` or `clp_last_seen(field, timestamp)` on the default event table:

```sql
SELECT clp_first_seen(_events.error.retryable, _events.ts) AS first_seen
FROM discovery_demo.events;
```

This returns `2026-11-24 10:00:01.456`. Use `clp_last_seen` instead to get `2026-11-24 10:00:03.012`. Request both and restrict the events with a normal `WHERE` clause:

```sql
SELECT clp_first_seen(_events.error.retryable, _events.ts) AS first_seen,
       clp_last_seen(_events.error.retryable, _events.ts) AS last_seen
FROM discovery_demo.events
WHERE _events.service::VARCHAR = 'payments';
```

CLP computes only the bounds your query uses. Requesting both shares the work. It uses archive summaries when they can answer the filtered query exactly, and reads event data when necessary. The result is exact in either case.

Pass the field path without a cast so CLP can test its presence. The second argument chooses the event clock: `_events.ts` uses the timestamp declared during compression and returns `TIMESTAMP_NS`. For an ordinary numeric clock, convert its known unit explicitly, for example `epoch_ms(_events.ts::BIGINT)`. Direct access uses the same expressions with `FROM clp_search('<archive path>')`.

To discover the history of every field at once, use the field inventory:

```sql
SELECT field_path, first_seen, last_seen
FROM clp_dataset_fields('discovery_demo', time_path := 'ts');
```

This also returns `source_types`, `matching_rows` and `archive_count`. It accepts field patterns, time windows and `predicate` as described below. Select only `first_seen` or `last_seen` when that is all you need. For direct access, use `clp_archive_fields` with an archive path or list of paths.

First and last seen refer to event time in the available data. A field present with a null value still counts. A missing field does not. Bounds are NULL when no matching event has both the field and a usable timestamp. In the field inventory, a numeric window (`start_value` or `end_value`) instead returns `BIGINT` bounds in the clock's original unit.

### Add time and field filters

You can combine event conditions with required fields and a time window:

```sql
SELECT schema_tree, matching_rows
FROM clp_dataset_schemas('discovery_demo',
    fields := ['error.code', 'error.retryable'],
    match_all := true,
    time_path := 'ts',
    start_time := TIMESTAMP '2026-11-24 10:00:00',
    end_time := TIMESTAMP '2026-11-24 10:00:02',
    predicate := $$ _events.service::VARCHAR = 'payments' $$);
```

This selects the payment error at `10:00:01.456`, returning one schema with `matching_rows = 1`. Time windows include the start and exclude the end. Either bound can be omitted.

### Filter parameters

| Parameter | What it selects |
|---|---|
| `fields := ['error.code']` | Schemas containing an exact document path, including a field whose value is null. |
| `field_patterns := ['error.%']` | Paths matching case-sensitive SQL LIKE patterns. `%` matches any sequence and `_` one character. |
| `match_all := true` | Require every field selector to match in the same schema. The default accepts any selector. |
| `predicate := $$ ... $$` | Events for which a scalar SQL condition evaluates to TRUE. |
| `time_path`, `start_time`, `end_time` | A time window for a field declared as a timestamp during compression. Supply typed timestamp literals. |
| `time_path`, `start_value`, `end_value` | A window for an ordinary signed integer field, using its original unit. |

Use timestamp bounds for fields declared as timestamps during compression. For relative device clocks and ordinary signed integers, use `start_value` and `end_value` in the field's original unit.

### Schemas and counts

To see which fields occur together, keep the schema rows separate:

```sql
SELECT schema_id, fields, matching_rows
FROM clp_dataset_schemas('discovery_demo')
ORDER BY matching_rows DESC;
```

The example has three schemas: two errors with `retryable`, one error without it and one successful event. Their counts are 2, 1 and 1. `schema_id` identifies a set of document paths and types.

| Result | Meaning |
|---|---|
| `matching_rows` | Exact count of events passing all selections for this schema. |
| `total_rows` | Records before event filtering for the schema groups that produced matches. This can exceed `matching_rows`. |
| `archive_count` | Number of archives contributing matches. |

Field filters return the complete schema, including fields beyond those requested. Expanding `fields` repeats the counts once per field. Sum `matching_rows` before expanding if you need an event total.

### Paths, types and predicates

A literal dot in a field name is escaped in discovery paths: `error\.code` means a single top-level key named `error.code`. Unescaped `error.code` follows the nested object. Arrays are reported as `array`, without expanding their individual elements.

Supported typed leaf accesses select values of the requested type and range. For example, `::BIGINT` returns NULL for strings, missing fields and incompatible values. `TRY_CAST(... AS BIGINT)` opts into conversion when needed. Inside `predicate`, use original document paths even if a typed view gives those paths different column names.

`predicate` supports comparisons, `IN`, `AND`/`OR`/`NOT`, arithmetic, null checks and scalar functions such as `regexp_matches`. It can inspect objects and arrays too, for example `_events.error IS NOT NULL`. Aggregates, window functions, subqueries and unbound parameters are not supported in this per-event condition.

Types describe the stored schema groups, so they can be broader than the values selected by a predicate. For a field containing both signed and unsigned integers, both types can still be listed after filtering selects only one.

### Direct archive access

Use a catalog for repeated work. For a one-off inspection, pass explicit archive paths to `clp_archive_schemas`. The sample command creates generated archive filenames, which Python can collect:

```python
from pathlib import Path

archives = sorted(str(path.resolve()) for path in Path('.work/discovery/archives').iterdir())
db.execute("""
    SELECT schema_tree, matching_rows
    FROM clp_archive_schemas(?, predicate := $$
        _events.service::VARCHAR = 'payments'
        AND _events.status::BIGINT >= 500
    $$)
""", [archives]).fetchall()
```

This returns the same schema and count as the catalog query. A single archive path also works. Direct schema discovery accepts explicit paths or a list of paths, rather than a directory or glob. Use `clp_register` when you want directory discovery and a persistent catalog.

### String shapes

A schema describes document structure and types. A string shape describes the repeated text pattern inside one field. For the sample catalog, the two error messages with different request numbers share a shape:

```sql
SELECT _shape.message::VARCHAR AS shape, count(*) AS occurrences
FROM discovery_demo.events
WHERE _events.service::VARCHAR = 'payments'
  AND _events.status::BIGINT >= 500
GROUP BY shape;
```

This returns `Request %int% failed` with a count of 2. `_shape_all.message` also derives shapes for strings stored whole. Use schema discovery to choose fields, then shape analysis to explore patterns within those fields. See [string-shape queries](../README.md#group-log-messages-by-string-shape) for more examples.

### Supported inputs

Schema discovery accepts whole CLP archives. These functions do not support catalogs containing only KV-IR inputs or individual archive-member entries.
