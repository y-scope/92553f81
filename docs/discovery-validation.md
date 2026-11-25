# Discovery on infrastructure metrics and CAN signals

Schema discovery, string shapes and field history were checked against every source JSONL record in both benchmark datasets on November 24, 2026. **Catalog and direct queries returned the expected results.** Numeric field history preserves the original clock units, without timestamp conversion.

| Dataset | Records checked | Source files | CLP archives | Discovered fields |
|---|---:|---:|---:|---:|
| Westermo infrastructure metrics | 1,640,857 | 19 | 1 | 25 |
| Public RAV4 CAN signals | 27,400,285 | 54 | 4 | 90 |

## What the queries tell us

The complete CAN dataset has 90 fields. Selecting events with `WHEEL_SPEED_FR >= 100` reduces the merged schema to seven fields: the four wheel speeds, `ts`, `channel_id` and `source_file`. Both schema result formats describe those same fields. A typed view can expose fewer columns while discovery still reports the underlying documents' complete schema.

First/last seen is useful with relative clocks too:

| Dataset and selection | Field | First seen | Last seen | Clock unit |
|---|---|---:|---:|---|
| Westermo, `system_id = 'system-1'` | `load_1m` | 0 | 2592000 | seconds |
| CAN, `source_file = 'rav4_c01_s0001'` | `WHEEL_SPEED_FR` | 9734164 | 9794150 | milliseconds |

These bounds describe the selected recording. They do not establish when a signal was introduced across the vehicle fleet or application history. Both datasets use relative clocks, so scope queries to a source when clocks may reset.

The string fields here are identifiers: 19 system names and 382 recording names. `clp_string_shape` returns these strings unchanged, and their grouped counts match the source data. These datasets validate that behavior. The [discovery walkthrough](duckdb-agentic-helper-features.md#find-repeated-string-shapes) demonstrates a text pattern shared by multiple messages.

## Query latency

These are catalog query P50 times from seven measured passes after two untimed passes. Timing includes executing SQL and fetching its results. It excludes starting DuckDB, loading the extension and registering archives. Direct access was checked for identical results. These are local measurements, not a comparison with the customer's faster machine.

| Operation | Westermo | CAN |
|---|---:|---:|
| List all fields | 1.2ms | 1.8ms |
| Return the full schema tree | 1.2ms | 2.4ms |
| Discover fields after a signal threshold filter | 11.5ms | 15.7ms |
| Discover fields after comparing two signals | 23.4ms | 22.0ms |
| Discover fields in one source's time window | 118.2ms | 178.9ms |
| First and last seen of one field within one source | 116.3ms | 173.1ms |
| First and last seen of every field within one source | 119.4ms | 2173.9ms |
| Group string shapes and count occurrences | 1.1ms | 1.3ms |

The threshold queries use `load_1m >= 2` and `WHEEL_SPEED_FR >= 100`. The two-signal queries compare `load_1m > load_15m` and `WHEEL_SPEED_FR > WHEEL_SPEED_FL`. Time windows cover Westermo's first hour and one second of the selected CAN recording, with a non-null signal condition.

Discovering all fields is inexpensive. Filtered discovery may scan values to identify the matching schemas. For field history, asking about a known field avoids the cost of inventorying every field, which is substantial on CAN. Request just first seen or last seen when only one bound is needed.

## Repeat the checks

After completing the [metrics benchmark](../README.md#run), use its run directory:

```bash
.venv/bin/python scripts/verify_discovery.py \
  --run .work/metrics/runs/<run>
```

The script reuses the archives, creates a separate catalog and checks results against the full original JSONL inputs. It covers both schema formats, event-value and time predicates, empty selections, filtered field paths, first-only/last-only/both bounds, string-shape counts, typed views and bound parameters. It does not change the benchmark's catalog or archives.

Results, SQL, query plans and timing samples are saved in the output directory printed by the script. [Saved results for this run](results/discovery-validation.json) include the SQL and expected values for every measured case. The twelve walkthrough SQL examples in the discovery guide were also executed successfully, and its CAN example matches the full-data check above.
