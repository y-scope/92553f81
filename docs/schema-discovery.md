# Discover schemas, string shapes and field history

CLP records are documents. Each document can have nested fields, and the fields and their types can vary between records. Three discovery features help you understand the data before querying it:

| Feature | What it tells you | SQL |
|---|---|---|
| [Schema discovery](#discover-fields-and-document-structure) | Which fields and types exist, and how they are nested. | `clp_schema()`, `clp_schema_tree()` |
| [String shapes](#find-repeated-string-shapes) | Which text patterns repeat within a field, and how often. | `clp_string_shape(field)` |
| [Field history](#find-when-a-field-first-or-last-appears) | When a field first or last appears in the selected events. | `clp_first_seen`, `clp_last_seen` |

All three support event filters, so you can explore the full dataset or a subset. The walkthrough uses four request events in `discovery_demo`: one successful payment request, two failed payment requests and one failed checkout request. Failed requests have a nested `error` object. [Prepare the example catalog](#prepare-the-example) to follow along, or substitute your own catalog and fields.

## Discover fields and document structure

The two APIs discover the same merged schema in different result formats:

| API | Result format | Useful for |
|---|---|---|
| `clp_schema()` | Rows with `field_path` and `source_types` columns. | Listing fields, choosing types for queries, or processing the schema as rows. |
| `clp_schema_tree()` | A nested VARIANT with types at each node. | Exploring document structure or accessing a particular branch. |

Both take **zero arguments**. `FROM` identifies the catalog event table or direct archive source, and `WHERE` selects the events to inspect.

A catalog can cover many archive files with different event structures. **CLP automatically merges their schemas**, using only schemas that contribute matching events. Each field path appears once with its combined types, including fields found in only some files. Direct access through `clp_search` merges schemas in the same way.

### Field rows: `clp_schema()`

```sql
SELECT clp_schema()
FROM discovery_demo.events
ORDER BY field_path;
```

This directly returns one row per field, with two columns:

| field_path | source_types |
|---|---|
| error | [object] |
| error.code | [string] |
| error.retryable | [boolean] |
| latency_ms | [float] |
| message | [string] |
| service | [string] |
| status | [integer] |
| ts | [timestamp] |

`error.code` means the `code` field inside the `error` object. If that path contains integers in one archive and strings in another, discovery returns one `error.code` row with `source_types = [integer, string]`. The tree has both types at that same node. A field in the merged schema does not have to exist in every event or every file.

### Nested tree: `clp_schema_tree()`

Keep the same `FROM` and use `clp_schema_tree()` to get the same schema as a nested tree:

```sql
SELECT clp_schema_tree() AS schema_tree
FROM discovery_demo.events;
```

The result is a VARIANT. Here it is formatted as JSON for readability:

```json
{
  "types": ["object"],
  "children": {
    "error": {
      "types": ["object"],
      "children": {
        "code": {"types": ["string"], "children": {}},
        "retryable": {"types": ["boolean"], "children": {}}
      }
    },
    "latency_ms": {"types": ["float"], "children": {}},
    "message": {"types": ["string"], "children": {}},
    "service": {"types": ["string"], "children": {}},
    "status": {"types": ["integer"], "children": {}},
    "ts": {"types": ["timestamp"], "children": {}}
  }
}
```

Each node has a `types` list and a `children` object. A leaf has no children. The `error` branch contains the same `code` and `retryable` fields shown as dotted paths in the field list. To return just that branch:

```sql
SELECT (clp_schema_tree()).children.error AS error_schema
FROM discovery_demo.events;
```

### Filter events before discovering their schema

Both formats support the same event filters. For example, discover the fields in failed payment requests:

```sql
SELECT clp_schema()
FROM discovery_demo.events
WHERE _events.service::VARCHAR = 'payments'
  AND _events.status::BIGINT >= 500;
```

This condition selects the two failed payment requests in the example. To return their schema as a tree, replace `clp_schema()` with `clp_schema_tree() AS schema_tree`. The selection stays the same.

Filters access document values through `_events`. Changing the status condition to `_events.status::BIGINT = 200` selects the successful request. Its schema has no `error` branch: `clp_schema()` omits the error paths, and `clp_schema_tree()` omits the error node. CLP resolves filters from archive metadata when possible and reads event data when necessary.

## Find repeated string shapes

A schema describes document structure and types. A string shape describes the repeated text pattern inside one field. In the example, `Request 11 failed` and `Request 13 failed` share the shape `Request %int% failed`. Group by that shape to count occurrences:

```sql
SELECT clp_string_shape(_events.message::VARCHAR) AS shape,
       count(*) AS occurrences
FROM discovery_demo.events
WHERE _events.service::VARCHAR = 'payments'
  AND _events.status::BIGINT >= 500
GROUP BY shape;
```

| shape | occurrences |
|---|---:|
| Request %int% failed | 2 |

`%int%` represents the varying integer. Shapes can also contain `%float%` and `%str%` variables. `clp_string_shape` returns `VARCHAR`. Missing, null and non-string values return NULL. With a typed view, pass its string column directly, for example `clp_string_shape(text)` when `text` maps to `message`.

CLP reads stored shapes and occurrence counts when they can answer the query, avoiding a scan of individual messages. Filters that select part of an archive may require scanning. The argument must refer to an original CLP archive field, through either catalog or direct access. Strings stored whole remain unchanged. See [string-shape queries](../README.md#group-log-messages-by-string-shape) for the `_shape` and `_shape_all` alternatives.

## Find when a field first or last appears

Use `clp_first_seen(field, order_value)` or `clp_last_seen(field, order_value)` on the default event table. They return the minimum or maximum ordering value among events containing the field. The ordering value can be a timestamp, relative clock or numeric counter:

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

| first_seen | last_seen |
|---|---|
| 2026-11-24 10:00:01.456 | 2026-11-24 10:00:03.012 |

CLP computes only the bounds your query uses. Requesting both shares the work. It uses archive summaries when they can answer the filtered query exactly, and reads event data when necessary. The result is exact in either case.

Pass the field path without a cast so CLP can test its presence. The second argument chooses the ordering value:

| Second argument | Result |
|---|---|
| `_events.ts` | Uses the timestamp declared during compression and returns `TIMESTAMP_NS`. |
| `_events.elapsed::BIGINT` | Returns `BIGINT` in the original unit. Seconds remain seconds, milliseconds remain milliseconds, and counters remain counters. |
| `_events.elapsed::DOUBLE` | Returns `DOUBLE`, preserving fractional numeric values. |

For the public CAN catalog created by the [benchmark runner](../README.md#run), find when a signal appears within one recording using its relative millisecond clock directly:

```sql
SELECT clp_first_seen(_events.WHEEL_SPEED_FR, _events.ts::BIGINT) AS first_ms,
       clp_last_seen(_events.WHEEL_SPEED_FR, _events.ts::BIGINT) AS last_ms
FROM can.events
WHERE _events.source_file::VARCHAR = 'rav4_c01_s0001';
```

| first_ms | last_ms |
|---:|---:|
| 9734164 | 9794150 |

Numeric inputs retain their SQL type and do not require timestamp conversion or a particular unit. Scope relative clocks to their recording or device when they can reset. Direct access uses the same expressions with `FROM clp_search('<archive path>')`.

A field present with a null value still counts. A missing field does not. Bounds are NULL when no matching event has both the field and a usable ordering value. These bounds describe the available records, not necessarily when an application first introduced a field. To discover the history of many fields together, use the [field inventory](#discover-history-across-many-fields).

## Query the fields you found

The default table `discovery_demo.events` gives you access to the actual documents. For failed payment requests, select the error code and latency:

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
    SELECT (clp_schema_tree()).children.error
    FROM discovery_demo.events
    WHERE _events.service::VARCHAR = 'payments'
      AND _events.status::BIGINT >= 500
""").fetchone()[0]
print(json.dumps(error_schema, indent=2))
```

### Add time and field filters

Use ordinary SQL conditions to choose the events whose schema you want to inspect:

```sql
SELECT clp_schema_tree() AS schema_tree
FROM discovery_demo.events
WHERE TRY_CAST(_events.ts AS TIMESTAMP_NS) >= TIMESTAMP_NS '2026-11-24 10:00:00'
  AND TRY_CAST(_events.ts AS TIMESTAMP_NS) < TIMESTAMP_NS '2026-11-24 10:00:02'
  AND _events.service::VARCHAR = 'payments'
  AND _events.error.code::VARCHAR IS NOT NULL
  AND _events.error.retryable::BOOLEAN IS NOT NULL;
```

This selects the payment error at `10:00:01.456` and returns its complete schema. Replace `clp_schema_tree()` with `clp_schema()` to return field rows instead. The example includes the start and excludes the end of the window. Omit either condition for an open-ended window.

The sample's `ts` value is ISO timestamp text. `TRY_CAST` explicitly converts that text to a timestamp. For a typed view with a `TIMESTAMP_NS` column, compare the column directly, such as `event_time >= TIMESTAMP_NS '2026-11-24 10:00:00'`. This uses the declared timestamp's instant across archive precisions. For an ordinary numeric clock, compare its typed integer value in the original unit. Do not cast numeric milliseconds directly to `TIMESTAMP_NS`.

The field conditions above require non-null values of the specified types. To limit the **displayed schema paths**, put that filter outside the discovery query:

```sql
SELECT field_path, source_types
FROM (
    SELECT clp_schema()
    FROM discovery_demo.events
    WHERE _events.service::VARCHAR = 'payments'
      AND _events.status::BIGINT >= 500
) AS fields
WHERE field_path LIKE 'error.%'
ORDER BY field_path;
```

| field_path | source_types |
|---|---|
| error.code | [string] |
| error.retryable | [boolean] |

The inner `WHERE` chooses events. The outer `WHERE` chooses fields from their merged schema.

### Discover history across many fields

To discover the history of every field at once, use the field inventory:

```sql
SELECT field_path, first_seen, last_seen
FROM clp_dataset_fields('discovery_demo', time_path := 'ts');
```

The function also exposes `source_types`, `matching_rows` and `archive_count`. Select only `first_seen` or `last_seen` when that is all you need. For direct access, use `clp_archive_fields` with an archive path or list of paths.

These parameters apply to the bulk field-history functions:

| Parameter | Meaning |
|---|---|
| `fields := ['error.code']` | Return this exact path, including occurrences whose value is null. |
| `field_patterns := ['error.%']` | Return paths matching case-sensitive SQL LIKE patterns. `%` matches any sequence and `_` one character. |
| `match_all := true` | Require all selected paths to occur together. The default accepts any selector. |
| `predicate := $$ _events.status::BIGINT >= 500 $$` | Include only events satisfying this condition. Use original document paths. |
| `time_path := 'ts'` | Choose the field used for first/last seen. |
| `start_time`, `end_time` | Bound a declared timestamp using typed timestamp literals, inclusive start and exclusive end. |
| `start_value`, `end_value` | Bound an ordinary signed integer clock in its original unit. First/last seen then return `BIGINT` values in that unit. |

### Paths, types and predicates

A literal dot in a field name is escaped in discovery paths: `error\.code` means a single top-level key named `error.code`. Unescaped `error.code` follows the nested object. Arrays are reported as `array`, without expanding their individual elements.

Supported typed leaf accesses select values of the requested type and range. For example, `::BIGINT` returns NULL for strings, missing fields and incompatible values. `TRY_CAST(... AS BIGINT)` opts into conversion when needed.

`WHERE` supports comparisons, `IN`, `AND`/`OR`/`NOT`, arithmetic, null checks and scalar functions such as `regexp_matches`. It can inspect objects and arrays too, for example `_events.error IS NOT NULL`. Bound parameters work with the schema APIs. The bulk field-history `predicate` parameter accepts a scalar expression without subqueries or parameters.

Types describe the stored schema groups, so they can be broader than the values selected by a predicate. For a field containing both signed and unsigned integers, both types can still be listed after filtering selects only one.

### Direct archive access

Use a catalog for repeated work. For a one-off inspection, both schema APIs work with `clp_search`:

```sql
SELECT clp_schema_tree() AS schema_tree
FROM clp_search('.work/discovery/archives')
WHERE _events.service::VARCHAR = 'payments'
  AND _events.status::BIGINT >= 500;
```

This returns the same merged schema as the catalog query. `clp_search` accepts an archive, directory or list of inputs.

### Schema-discovery input and query restrictions

Schema discovery requires whole CLP archives. KV-IR inputs and individual archive-member entries are unsupported, including when mixed with whole archives in a catalog.

Use the zero-argument APIs in an ungrouped schema query over one event table or direct archive source. Put `clp_schema()` directly in the SELECT list to expose its two columns. They describe stored event fields, including fields omitted by a view's projection. Ordinary scalar filters, bound parameters and `FILTER (WHERE ...)` are supported. Joins, grouping, windows, row limits on the input events, volatile conditions and combining them with non-schema aggregates are not supported.

| API | Output | Result when no events match |
|---|---|---|
| `clp_schema()` | `field_path VARCHAR`, `source_types VARCHAR[]` | Zero rows. |
| `clp_schema_tree()` | One `VARIANT` column. | One row containing `{"types":["object"],"children":{}}`. |

Filter or aggregate discovered field rows in an [outer query](#add-time-and-field-filters). Selecting both result formats with the same event filter shares discovery work and repeats the tree alongside each field row.

### Verified on the public datasets

These operations were checked against all 1.64 million Westermo records and 27.40 million CAN frames, through both catalog and direct access. See [the validation results](discovery-validation.md) for concrete field histories, query timings and a script to repeat the checks.
