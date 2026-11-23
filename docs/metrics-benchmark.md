# CLP benchmark: infrastructure metrics and vehicle CAN signals

Measured November 23, 2026. This report evaluates CLP compression and DuckDB queries over two public datasets: Westermo infrastructure metrics and comma.ai RAV4 CAN signals. Queries use catalogs with typed views and an on-disk decode cache. Results describe this host and workload.

- **Compression:** 30.8× for Westermo and 72.2× for public CAN, relative to the prepared NDJSON input.
- **Query latency:** 16 of 18 queries have P50 latency between 0.8ms and 2.1ms. The two slower queries are a full-input memory-ratio calculation at 59.9ms and an ordered CAN first-20 retrieval at 194.4ms.
- **Validation:** every query result matched an independent query over the source NDJSON, including all 126 measured executions.

## Datasets and coverage

| Dataset | Coverage | Records | NDJSON input files | CLP archives |
|---|---|---:|---:|---:|
| [Westermo test-system performance](https://github.com/westermo/test-system-performance-dataset) | All 19 systems, sampled twice per minute over about one month | 1,640,857 | 19 | 1 |
| [comma.ai comma2k19 RAV4](https://huggingface.co/datasets/commaai/comma2k19) | All 382 RAV4 segments in Chunks 1–2, spanning 42 routes | 27,400,285 | 54 | 4 |

Westermo includes CPU, load, memory, disk and other system metrics. Each CSV row becomes one NDJSON document with a system identifier. The queries exercise load thresholds, time windows, grouped statistics and memory calculations.

Public CAN records are wide documents: each contains the signals decoded from one CAN frame, its relative device time, channel and source segment. Files group records by CAN message ID and channel. The preparation includes received frames with a matching DBC definition and payload length, covering about 72.72% of received frames. It excludes send receipts and undecodable frames. This benchmark covers the full prepared RAV4 subset, not the entire comma2k19 collection.

The CAN queries focus on wheel-speed signals, present in 1,896,763 of the 27,400,285 records. Other signals remain in the archives. Product and ratio queries exercise the arithmetic shapes used in signal analytics; they are not physical energy-consumption calculations.

## Compression

Compression ratio is prepared NDJSON bytes divided by total CLP archive bytes, including archive metadata. It is not a comparison against the downloaded CSV, ZIP files or raw CAN payloads.

| Dataset | NDJSON input | CLP archive size (compression ratio) | Compression time | Input throughput | Peak compression RSS |
|---|---:|---:|---:|---:|---:|
| Westermo | 994.3MiB | 32.3MiB (30.8×) | 6.2s | 161.4MiB/s | 66.0MiB |
| Public CAN | 3,600.9MiB | 49.8MiB (72.2×) | 28.6s | 125.9MiB/s | 87.4MiB |

The archives contain the complete prepared NDJSON datasets, including fields not used by the queries. Compression time and peak memory cover the CLI process only. Download, conversion, hashing, reference queries and catalog setup are excluded.

## Query performance

Each dataset has a separate **64MiB on-disk decode cache**, shared across its entire workload. Two complete warm-up passes precede seven measured passes, with query order rotated and reversed. The cache stays enabled throughout.

**Timing includes SQL planning, execution and complete result delivery through the DuckDB Python client.** DuckDB startup, extension loading, catalog registration, warm-up and validation are outside the timed interval.

Tables show the average, P50 (median) and P95 across seven executions of each query. Average reflects total execution cost, P50 shows typical latency, and P95 highlights slower repetitions. With seven samples, P95 is a rough indicator, not a production tail-latency estimate.

### Westermo infrastructure metrics

| Query | Operation | Average | P50 | P95 |
|---|---|---:|---:|---:|
| [W1](#w1) | Count all samples | 0.8ms | 0.8ms | 0.8ms |
| [W2](#w2) | Count samples with one-minute load ≥2 | 0.8ms | 0.8ms | 0.8ms |
| [W3](#w3) | Count samples for one system in a one-hour window | 0.9ms | 0.8ms | 1.1ms |
| [W4](#w4) | Count, average, minimum and maximum one-minute load | 0.9ms | 0.8ms | 1.3ms |
| [W5](#w5) | Count samples and average CPU and available memory per system | 1.8ms | 1.6ms | 2.5ms |
| [W6](#w6) | Average `1 − available memory / total memory` | 61.4ms | 59.9ms | 67.7ms |
| [W7](#w7) | Return the five systems with highest average 15-minute load | 1.8ms | 2.1ms | 2.4ms |
| [W8](#w8) | Filter load ≥2 and CPU >0.1, then return the first 20 ordered samples | 1.2ms | 1.2ms | 1.3ms |

The one-hour query returns 120 samples. Full-dataset load statistics report an average of 0.130320 and a maximum of 12.59.

### Public CAN signals

| Query | Operation | Average | P50 | P95 |
|---|---|---:|---:|---:|
| [S1](#s1) | Count frames containing front-right wheel speed | 1.6ms | 1.6ms | 2.6ms |
| [S2](#s2) | Count front-right wheel speeds ≥100km/h | 1.0ms | 1.0ms | 1.2ms |
| [S3](#s3) | Count wheel-speed frames for one segment in a one-second window | 1.1ms | 1.1ms | 1.3ms |
| [S4](#s4) | Count, average, minimum and maximum front-right wheel speed | 0.9ms | 0.9ms | 1.0ms |
| [S5](#s5) | Sum front-right × front-left wheel speeds | 0.8ms | 0.8ms | 0.9ms |
| [S6](#s6) | Divide the product sum by the rear-right wheel-speed sum, ×100 | 0.9ms | 0.9ms | 1.0ms |
| [S7](#s7) | Maximum minus minimum front-right wheel speed | 0.9ms | 0.8ms | 0.9ms |
| [S8](#s8) | Compute the S6 ratio for one segment in a one-second window | 1.2ms | 1.2ms | 1.4ms |
| [S9](#s9) | Return the five segments with most wheel-speed frames | 2.0ms | 1.8ms | 2.6ms |
| [S10](#s10) | Return the first 20 wheel-speed frames ordered by source, time and speed | 192.2ms | 194.4ms | 197.8ms |

The one-second window returns 83 wheel-speed frames. Across the full subset, front-right wheel speed averages 81.985687km/h and reaches 132.09km/h. The exact executed SQL and selected results are in the [query appendix](#appendix).

### Why two queries take longer

**W6, average memory ratio:** this expression reads two numeric columns over all 1.64 million samples and performs the division in DuckDB. Its decoded column data exceeds the default cache-entry ceiling, so the 64MiB total budget does not make it fully cached. Its P50 is 59.9ms.

**S10, ordered first-20 retrieval:** `LIMIT 20` limits the output, but the source/time/speed ordering still requires examining approximately 1.90 million qualifying wheel-speed frames. This is more work than returning any 20 matches. Its projected data also exceeds the per-entry ceiling. Its P50 is 194.4ms.

These two cases remain opportunities for further optimization. Their SQL and cache settings are unchanged in the reported run.

## Decode-cache use

The decode cache stores reusable metadata, decoded column data and per-archive aggregate partials. It does not store the final SQL result. The query is executed again for every measured repetition. The default **4MiB per-entry limit** remains in effect, independently of the 64MiB total capacity.

| Dataset | Capacity | Actual stored data | Entries | Per-entry limit |
|---|---:|---:|---:|---:|
| Westermo | 64MiB | 18.9KiB | 6 | 4MiB |
| Public CAN | 64MiB | 35.4KiB | 39 | 4MiB |

Stored bytes and entry counts were unchanged between the end of warm-up and the end of measurement. The small cache footprint reflects reuse of compact metadata and partial aggregates for many of these queries. W6 and S10 still perform substantial scan work because their columns do not fit in the cache.

## Reproduce the benchmark

From the repository root, run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements-metrics.txt
.venv/bin/python scripts/benchmark_metrics.py
```

The runner downloads the public inputs, prepares NDJSON, compresses both datasets, creates catalogs and typed views, validates results and writes a new report. See the [dataset and benchmark guide](metrics-datasets.md) for requirements, source revisions, preparation coverage and individual-dataset commands.

The [saved measurements](results/metrics-2026-11-23.json) include exact byte counts, every query result, all timing samples, settings and binary checksums for this report. The executable query definitions are [Westermo](../scripts/queries/westermo.json) and [CAN](../scripts/queries/can.json). No earlier small-sample timings are mixed into these results.

## Appendix

### Measurement settings and validation

| Setting | Recorded value |
|---|---|
| Platform | Linux x86_64, 16 logical CPUs |
| CLP CLI / DuckDB | `clp-s 1.0.0-xp` / DuckDB 1.5.6 |
| Query execution | Catalog typed views, 8 DuckDB threads, 4GB memory limit |
| Compression | Column Zstd level 3, dictionary level 9 |
| Archive rotation | Target 512MiB of encoded input per archive |
| Query repetitions | Two full warm-ups, seven measured workload passes |
| Compression repetitions | One complete compression per dataset |

The runner reads the prepared NDJSON with DuckDB’s own JSON reader in a separate connection without the CLP extension, then evaluates the same queries as an independent reference. Counts and integers must match exactly. Floating-point outputs use relative tolerance `1e-9` and absolute tolerance `1e-6` to allow summation-order differences. All measured queries passed. This verifies these query results, not a field-by-field round trip of every record.

### Typed views and relative clocks

Westermo queries use `westermo.metrics`. Source metric names have hyphens replaced by underscores. The mapped view renames `timestamp` to `elapsed_s`, `sys_mem_total` to `memory_total`, and `sys_mem_available` to `memory_available`.

| Westermo column | SQL type |
|---|---|
| `system_id` | `VARCHAR` |
| `elapsed_s`, `memory_total`, `memory_available` | `BIGINT` |
| `load_1m`, `load_15m`, `cpu_user` | `DOUBLE` |

CAN queries use `can.signals`. The signal names remain unchanged. Missing signals project as NULL.

| CAN column | SQL type |
|---|---|
| `source_file` | `VARCHAR` |
| `ts`, `channel_id` | `BIGINT` |
| `WHEEL_SPEED_FR`, `WHEEL_SPEED_FL`, `WHEEL_SPEED_RR` | `DOUBLE` |

`elapsed_s` is relative seconds, and CAN `ts` is relative device milliseconds. They are ordinary integers, not Unix timestamps. Window queries include a system or source-segment predicate so unrelated clocks are not mixed. All windows below use an inclusive lower bound and exclusive upper bound.

### Westermo SQL

#### W1

Count all samples.

```sql
SELECT count(*) AS n
FROM westermo.metrics;
```

Checked result: 1,640,857 samples.

#### W2

Count samples with one-minute load ≥2.

```sql
SELECT count(*) AS n
FROM westermo.metrics
WHERE load_1m >= 2.0;
```

Checked result: 2,663 samples.

#### W3

Count samples for one system in a one-hour window.

```sql
SELECT count(*) AS n
FROM westermo.metrics
WHERE system_id='system-1'
  AND elapsed_s >= 0
  AND elapsed_s < 3600;
```

Checked result: 120 samples from `system-1` in `[0, 3600)` seconds.

#### W4

Count, average, minimum and maximum one-minute load.

```sql
SELECT count(load_1m) AS n, avg(load_1m) AS average, min(load_1m) AS minimum, max(load_1m) AS maximum
FROM westermo.metrics;
```

Checked result: 1,640,857 values. Average 0.130320, minimum 0, maximum 12.59.

#### W5

Count samples and average CPU and available memory per system.

```sql
SELECT system_id, count(*) AS n, avg(cpu_user) AS cpu_user, avg(memory_available) AS memory_available
FROM westermo.metrics
GROUP BY system_id
ORDER BY system_id;
```

Checked result: 19 system rows. For `system-1`, 85,749 samples, average CPU value 0.060740 and average available memory about 15.59GB.

#### W6

Average `1 − available memory / total memory`.

```sql
SELECT avg(1.0 - memory_available::DOUBLE / memory_total) AS fraction
FROM westermo.metrics
WHERE memory_total > 0;
```

Checked result: 0.102393, or about 10.24% unavailable for allocation.

#### W7

Return the five systems with highest average 15-minute load.

```sql
SELECT system_id, avg(load_15m) AS average
FROM westermo.metrics
GROUP BY system_id
ORDER BY average DESC NULLS LAST, system_id
LIMIT 5;
```

Checked result: Five systems. `system-11` has the highest average, approximately 0.177341.

#### W8

Filter load ≥2 and CPU >0.1, then return the first 20 ordered samples.

```sql
SELECT system_id, elapsed_s, load_1m, cpu_user
FROM westermo.metrics
WHERE load_1m >= 2.0
  AND cpu_user > 0.1
ORDER BY system_id, elapsed_s, load_1m, cpu_user
LIMIT 20;
```

Checked result: 20 rows. The first is from `system-1` at 23,610 seconds, with load 2.05.

### Public CAN SQL

#### S1

Count frames containing front-right wheel speed.

```sql
SELECT count(*) AS n
FROM can.signals
WHERE WHEEL_SPEED_FR IS NOT NULL;
```

Checked result: 1,896,763 frames.

#### S2

Count front-right wheel speeds ≥100km/h.

```sql
SELECT count(*) AS n
FROM can.signals
WHERE WHEEL_SPEED_FR >= 100.0;
```

Checked result: 907,075 frames.

#### S3

Count wheel-speed frames for one segment in a one-second window.

```sql
SELECT count(*) AS n
FROM can.signals
WHERE WHEEL_SPEED_FR IS NOT NULL
  AND source_file='rav4_c01_s0001'
  AND ts >= 9734164
  AND ts < 9735164;
```

Checked result: 83 frames from `rav4_c01_s0001` in `[9734164, 9735164)` milliseconds.

#### S4

Count, average, minimum and maximum front-right wheel speed.

```sql
SELECT count(WHEEL_SPEED_FR) AS n, avg(WHEEL_SPEED_FR) AS average, min(WHEEL_SPEED_FR) AS minimum, max(WHEEL_SPEED_FR) AS maximum
FROM can.signals;
```

Checked result: 1,896,763 values. Average 81.985687km/h, minimum 0km/h, maximum 132.09km/h.

#### S5

Sum front-right × front-left wheel speeds.

```sql
SELECT sum(WHEEL_SPEED_FR*WHEEL_SPEED_FL) AS value
FROM can.signals;
```

Checked result: Approximately 15,361,045,528.80.

#### S6

Divide the product sum by the rear-right wheel-speed sum, ×100.

```sql
SELECT sum(WHEEL_SPEED_FR*WHEEL_SPEED_FL)/nullif(sum(WHEEL_SPEED_RR),0)*100 AS value
FROM can.signals;
```

Checked result: Approximately 9,898.768707.

#### S7

Maximum minus minimum front-right wheel speed.

```sql
SELECT max(WHEEL_SPEED_FR)-min(WHEEL_SPEED_FR) AS value
FROM can.signals;
```

Checked result: 132.09km/h.

#### S8

Compute the S6 ratio for one segment in a one-second window.

```sql
SELECT sum(WHEEL_SPEED_FR*WHEEL_SPEED_FL)/nullif(sum(WHEEL_SPEED_RR),0)*100 AS value
FROM can.signals
WHERE source_file='rav4_c01_s0001'
  AND ts >= 9734164
  AND ts < 9735164;
```

Checked result: Approximately 12,052.243415.

#### S9

Return the five segments with most wheel-speed frames.

```sql
SELECT source_file, count(*) AS n
FROM can.signals
WHERE WHEEL_SPEED_FR IS NOT NULL
GROUP BY source_file
ORDER BY n DESC, source_file
LIMIT 5;
```

Checked result: Five segments. Four tie at 4,976 wheel-speed frames. Source name breaks ties.

#### S10

Return the first 20 wheel-speed frames ordered by source, time and speed.

```sql
SELECT source_file, ts, WHEEL_SPEED_FR, WHEEL_SPEED_FL
FROM can.signals
WHERE WHEEL_SPEED_FR IS NOT NULL
ORDER BY source_file, ts, WHEEL_SPEED_FR, WHEEL_SPEED_FL
LIMIT 20;
```

Checked result: 20 rows. The first is from `rav4_c01_s0001` at 9,734,164ms, with front-right speed 120.3km/h and front-left speed 120.18km/h.
