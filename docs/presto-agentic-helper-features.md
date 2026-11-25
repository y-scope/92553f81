# Agentic helper features in Presto

A CLP event is a document with nested fields. Different events can have different fields, and the same field can hold different types. A Presto table's declared columns are a convenient typed view of that data. The discovery functions inspect the schemas stored in the archives, including fields that have not been declared as table columns.

| Investigation question | Helper |
| --- | --- |
| What fields and types are available in the matching events? | [`clp_schema()` or `clp_schema_tree()`](#discover-the-schema) |
| Which fields are relevant to my investigation? | [`clp_schema_semantic_search('intent')`](#find-relevant-fields) |
| What is the earliest or latest ordering value where a field exists? | [`clp_first_seen(path, order_value)` or `clp_last_seen(path, order_value)`](#find-when-a-field-appears) |
| Which message patterns repeat, and how often? | [`clp_string_shape(field)` with `GROUP BY`](#group-messages-by-string-shape) |
| Which messages relate to a natural-language description? | [`clp_semantic_match(field, query, threshold)` in `WHERE`](#search-by-meaning) |

These examples require a Presto deployment with the updated CLP connector. This repository bundles the DuckDB extension and CLI; obtain the Presto connector from YScope.

Examples use a CLP table named `clp.default.discovery_demo` containing the [four request events](duckdb-agentic-helper-features.md#prepare-the-example), with `message`, `service` and `status` exposed as typed columns. Register those archives in your Presto deployment or substitute your own table and fields. Presto merges the selected archives' schemas, including with S3 storage and Metalog catalogs. The [DuckDB guide](duckdb-agentic-helper-features.md) covers its corresponding syntax.

## Discover the schema

Two functions describe the same selected data in different formats:

| Function | Result |
| --- | --- |
| `clp_schema()` | A `MAP(VARCHAR, ARRAY(VARCHAR))` from field path to its stored source types. |
| `clp_schema_tree()` | One JSON object with nested `children` and a `types` array at each node. |

```sql
SELECT clp_schema() AS fields
FROM clp.default.discovery_demo;
```

This returns one row containing the field inventory. To display each field as a separate result row, use Presto's `UNNEST`:

```sql
SELECT field_path, source_types
FROM (
    SELECT clp_schema() AS fields
    FROM clp.default.discovery_demo
)
CROSS JOIN UNNEST(fields) AS f(field_path, source_types)
ORDER BY field_path;
```

For example, part of the result is:

| field_path | source_types |
| --- | --- |
| error | [object] |
| error.code | [string] |
| error.retryable | [boolean] |
| message | [string] |
| latency_ms | [float] |
| ts | [timestamp] |

`null` means an explicitly stored null value. A missing field contributes no type. Arrays appear as containers, rather than one field per element. Paths escape literal dots and backslashes, so `odd\.key.child` means the `child` field inside the top-level key `odd.key`.

For a nested view:

```sql
SELECT clp_schema_tree() AS schema_tree
FROM clp.default.discovery_demo;
```

The JSON contains nodes such as:

```json
{
  "types": ["object"],
  "children": {
    "error": {
      "types": ["object"],
      "children": {
        "code": {"types": ["string"]},
        "retryable": {"types": ["boolean"]}
      }
    }
  }
}
```

This excerpt shows only the `error` branch. The full result includes the other fields.

### Discover the schema of matching events

Put the event conditions in the same query as the discovery function:

```sql
SELECT clp_schema_tree()
FROM clp.default.discovery_demo
WHERE service = 'payments' AND status >= 500;
```

Declared columns and `CLP_GET_*` accessors both work. For example, this query restricts discovery to a timestamp window:

```sql
SELECT clp_schema()
FROM clp.default.discovery_demo
WHERE CLP_GET_TIMESTAMP('ts') >= TIMESTAMP '2026-11-24 10:00:01'
  AND CLP_GET_TIMESTAMP('ts') < TIMESTAMP '2026-11-24 10:00:02';
```

Normal SQL predicates, including arithmetic, functions, `IN`, and expressions over `CLP_GET_JSON_STRING()`, apply before schemas are merged. An ordinary predicate that cannot be pushed into the reader remains a Presto filter. Semantic matching must execute in the archive reader, as described in [search by meaning](#search-by-meaning). Use typed leaf accessors when possible to avoid reconstructing complete documents.

The result merges the stored schemas that contribute at least one matching event. It is not a fresh type census of the matching values. A stored schema can describe multiple numeric types even when the selected values use only one of them. An empty selection returns an empty field map, or the tree `{"types":["object"],"children":{}}`.

Filter the result of `UNNEST` to inspect specific paths, for example `WHERE field_path LIKE 'error.%'`. This selects entries from the discovered inventory. It does not filter source events.

## Find relevant fields

Use `clp_schema_semantic_search` when you know the investigation goal but do not know the field names:

```sql
SELECT field_path, source_types, relevance, matched_text
FROM (
    SELECT clp_schema_semantic_search('error classification and retry status') AS fields
    FROM clp.default.discovery_demo
    WHERE service = 'payments'
)
CROSS JOIN UNNEST(fields) AS f(field_path, source_types, relevance, matched_text)
ORDER BY relevance DESC, field_path
LIMIT 10;
```

The function returns `ARRAY(ROW(field_path VARCHAR, source_types ARRAY(VARCHAR), relevance DOUBLE, matched_text VARCHAR))`. `UNNEST` displays one candidate per row. Without it, the query returns the ranked array in one cell.

`field_path` is an existing document path, `source_types` contains its distinct observed types, and `matched_text` shows the descriptor supplied to the semantic service. Higher `relevance` means greater model similarity, not probability. Use a returned path with `CLP_GET_STRING`, `CLP_GET_BIGINT`, or another getter appropriate to its type in the next query.

The inner `WHERE` selects the events whose schema is searched. Presto merges their schemas across the selected archives before scoring, so the outer `LIMIT` ranks fields across the whole scope. Empty selections return no candidates. Local and S3 archives, including Metalog-managed tables, use the same path.

Descriptors contain the full path, split identifier words, and source types. For example, `db.connectionPool.waitMs` contributes `db connection pool wait ms`. This operation does not send event values or message text. Opaque names such as `signal_001` remain opaque without human descriptions. It suggests fields to inspect and does not execute a follow-up query.

The intent must be a nonempty constant of at most 512 Unicode characters. Descriptors use their first 512 characters, as shown in `matched_text`. The native worker uses the same [semantic service](#semantic-service) as `clp_semantic_match`. Service errors fail the query rather than producing a partial ranking.

## Find when a field appears

Supply the observed field's path and the value to order by:

```sql
SELECT clp_first_seen('error.code', CLP_GET_TIMESTAMP('ts')) AS first_seen,
       clp_last_seen('error.code', CLP_GET_TIMESTAMP('ts')) AS last_seen
FROM clp.default.discovery_demo;
```

| first_seen | last_seen |
| --- | --- |
| 2026-11-24 10:00:01.456 | 2026-11-24 10:00:03.012 |

The first argument is a constant escaped field path. The second is an orderable SQL expression. Ordinary numeric clocks remain ordinary numbers in their original units. For a timestamp declared during compression, use a mapped `TIMESTAMP` column or `CLP_GET_TIMESTAMP`:

```sql
SELECT clp_first_seen('error.code', CLP_GET_TIMESTAMP('ts')) AS first_seen
FROM clp.default.discovery_demo
WHERE service = 'payments';
```

Use just `clp_first_seen` or just `clp_last_seen` when that is all you need. Both can also be selected together. Field presence includes explicit null, objects, and arrays. A missing field does not participate. A null ordering value is ignored. No eligible value produces SQL `NULL`.

These functions also support grouping and aggregate-specific filters:

```sql
SELECT service,
       clp_first_seen('error', CLP_GET_TIMESTAMP('ts')) AS first_error,
       clp_last_seen('error', CLP_GET_TIMESTAMP('ts')) AS last_error
FROM clp.default.discovery_demo
GROUP BY service;
```

## Group messages by string shape

A string shape keeps the fixed text of an encoded message and replaces its variables with placeholders. This makes related messages easier to count and inspect:

```sql
SELECT clp_string_shape(message) AS shape, count(*) AS occurrences
FROM clp.default.discovery_demo
WHERE status >= 500
GROUP BY 1
ORDER BY occurrences DESC;
```

An undeclared field works through its typed accessor:

```sql
SELECT clp_string_shape(CLP_GET_STRING('message')) AS shape, count(*) AS occurrences
FROM clp.default.discovery_demo
GROUP BY 1;
```

The argument must identify a CLP string field, either as a declared `VARCHAR` column or through `CLP_GET_STRING('path')`. Expressions such as `clp_string_shape(lower(message))` are rejected because they no longer identify the stored string. Missing fields and non-string values return null. Verbatim strings retain their value when the archive has no encoded string shape for them.

## Search by meaning

Use `clp_semantic_match` when you can describe a problem but do not know the exact message wording:

```sql
SELECT message, status
FROM clp.default.discovery_demo
WHERE clp_semantic_match(message, 'request failures', 0.6)
  AND service = 'payments';
```

Use a declared `VARCHAR` column or `CLP_GET_STRING('message')`. The query text and optional threshold must be constants. The threshold is inclusive, ranges from `0` to `1`, and defaults to `0.3`. Higher thresholds select fewer shapes. A score is model similarity, not a probability.

CLP sends the selected field's distinct string shapes and the query text to the semantic service. It then filters the matching encoded IDs locally. Events with the same stored shape share a match decision. Variables inside the shape are placeholders, so their individual values are not searched semantically. Strings stored without a shape are scored as complete dictionary values. Use ordinary predicates for exact IDs, numeric values, and time windows.

### Refine an investigation with the same predicate

Count the matching patterns, then inspect their document structure or field history:

```sql
SELECT clp_string_shape(message) AS shape, count(*) AS occurrences
FROM clp.default.discovery_demo
WHERE clp_semantic_match(message, 'request failures', 0.6)
  AND status >= 500
GROUP BY 1
ORDER BY occurrences DESC;

SELECT clp_schema_tree() AS schema_tree
FROM clp.default.discovery_demo
WHERE clp_semantic_match(message, 'request failures', 0.6)
  AND status >= 500;

SELECT clp_first_seen('message', CLP_GET_TIMESTAMP('ts')) AS first_seen,
       clp_last_seen('message', CLP_GET_TIMESTAMP('ts')) AS last_seen
FROM clp.default.discovery_demo
WHERE clp_semantic_match(message, 'request failures', 0.6)
  AND status >= 500;
```

Replace `clp_schema_tree()` with `clp_schema()` to get the field map. The history query returns timestamps. For an ordinary numeric counter or clock, use its typed column or `CLP_GET_BIGINT('path')` as the ordering value. Its original unit is preserved.

### Matching rules and service behavior

`AND`, `OR`, and `NOT` compose with pushed predicates. For schema and field-history discovery, combine ordinary conditions with `AND`. An `OR` in a discovery filter may contain only semantic predicates, and the selected SQL types must agree with the archive's source types. Missing, null, and non-string fields do not match, including under `NOT`.

Semantic matching must filter an archive string field in `WHERE`. Computed strings, projecting a score in `SELECT`, binary UUID columns, array-element matching, and live KV-IR streams are unsupported.

Query and shape text are limited to their first 512 Unicode characters. Service failures fail the query rather than dropping its filter. See [semantic search configuration](#semantic-service) for the endpoint, deadlines, and data sent to the service.

## Supported query shapes

Schema and field-history discovery require one CLP archive table beneath the aggregation, with optional projections and filters. Views and CTEs that expand to that form work. Joins, input limits, windows, and nested aggregation are rejected rather than silently changing the selected data. Ordering or limiting the completed discovery result is fine. String shapes also require a single source table. These discovery APIs require archives, not KV-IR streams.

## Semantic service

Both semantic helpers use the YScope service at `https://ca-central-semantic-cache.yscope.ai`. Every native worker must be able to reach it. Field discovery sends the intent and field descriptors. Message search sends the intent and stored shapes or complete strings, which can contain application information. Event values are not sent for field discovery.

Service errors fail the query instead of removing its filter or returning an incomplete ranking. Each operation has a bounded request deadline. Use ordinary SQL predicates for exact values and identifiers.
