# Public metrics datasets and benchmarks

The runner downloads public data, prepares NDJSON, compresses it, registers the archives in a DuckDB catalog and executes SQL queries. It reports compression ratio, compression throughput, peak compression memory and query latency. For other inputs, follow [Evaluate your own dataset](../README.md#evaluate-your-own-dataset).

## Choose a dataset

| Dataset | What it contains | Prepared benchmark input |
|---|---|---|
| [Westermo test-system performance](https://github.com/westermo/test-system-performance-dataset) | Load, CPU, memory, disk and other measurements from 19 systems over about one month | One NDJSON document per sampling time and system, converted from 379MB of CSV |
| [comma.ai comma2k19 RAV4 subset](https://huggingface.co/datasets/commaai/comma2k19) | Decoded vehicle CAN signals from Chunks 1 and 2 | 27,400,285 wide records across 54 message/channel tables, approximately 3.78GB of NDJSON |

These are real public observations. The CAN data is independent of customer Li Auto data. The RAV4 subset is part of the larger comma2k19 collection, which also includes Civic recordings.

Allow approximately **20GB** for one full run of both datasets, including downloaded inputs, conversion intermediates, reference tables and archives. Keep additional space for repeated runs. The CAN downloader uses HTTP ranges to fetch only the necessary members of the source ZIPs, skipping video and unrelated sensor data. It downloads roughly 4GB instead of the complete 17.8GB ZIPs. Wide records contain the signals belonging to that CAN frame, with unavailable fields omitted.

## Run the benchmark

From the repository root, using Linux x86_64, Python 3.11 or 3.12, virtual-environment support and GNU `time`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements-metrics.txt

# Both complete datasets.
.venv/bin/python scripts/benchmark_metrics.py

# Or run either dataset individually.
.venv/bin/python scripts/benchmark_metrics.py --dataset westermo
.venv/bin/python scripts/benchmark_metrics.py --dataset can
```

The Python DuckDB client is pinned to the version required by the bundled extension. A separate DuckDB CLI installation is unnecessary.

For a quick end-to-end check before the full download:

```bash
.venv/bin/python scripts/benchmark_metrics.py \
  --westermo-systems 1 --can-segments 2 --repeats 3
```

Sample runs are explicitly labelled by their system and segment counts. Their reference answers are computed from the actual selected input. Do not compare their compression or query numbers with full-dataset results.

### Defaults and measurement

| Setting | Default |
|---|---|
| Query route | Catalog with typed column views |
| Decode cache | Enabled, 64MiB on disk per dataset |
| Per-entry cache ceiling | 4MiB |
| Warm-up | Two complete passes over all queries |
| Measured repetitions | Seven passes, rotating query order |
| DuckDB threads | Up to eight, limited by available logical CPUs |
| DuckDB memory limit | 4GB |
| Compression | Column level 3, dictionary level 9 |
| Archive rotation target | 512MiB of encoded input per archive |

The runner inherits the extension's cache defaults without overriding enablement, capacity or the per-entry limit. It only selects a separate cache directory for each dataset, so each has its own budget shared by all its queries throughout warm-up and measurement. The cache stores reusable decoded data, metadata and aggregate partials, not the final SQL result. Queries can still decompress data when an entry is too large or the working set exceeds capacity. The report records the effective capacity, actual use and per-entry ceiling.

Query timing includes planning, execution and complete result delivery through the Python client. Process startup, extension loading, catalog registration, warm-up, correctness checks and report writing are excluded. Average, P50 and P95 summarize the measured repetitions of each query. P50 is the median.

Compression ratio is **prepared NDJSON bytes / total CLP archive bytes**, including metadata stored inside the archives. It is not a comparison with downloaded ZIPs or raw binary CAN payloads. Compression time and peak RSS cover the CLI process. Download, conversion, input verification and query execution are separate.

For example, to use a different results directory or run more repetitions:

```bash
.venv/bin/python scripts/benchmark_metrics.py --dataset westermo \
  --output .work/metrics/runs/westermo-run-1 --repeats 11
```

`--output` must name a new directory. Repeated runs verify and reuse downloads and prepared NDJSON, then create fresh archives. No existing results are overwritten. The runner serializes runs sharing a work directory. Avoid unrelated builds or benchmarks during measurement.

## Queries and validation

Westermo uses the `westermo.metrics` typed view. Its eight queries cover total counts, load thresholds, a one-hour window, count/average/minimum/maximum, grouped CPU and memory statistics, a memory-use expression, top-five systems and filtered projection.

CAN uses `can.signals`. Its ten queries cover signal presence, speed thresholds, a one-second window, count/average/minimum/maximum, products, ratios, ranges, top-five source segments and filtered projection. Product and ratio queries exercise arithmetic aggregation and are not physical energy calculations.

The executable definitions are in [westermo.json](../scripts/queries/westermo.json) and [can.json](../scripts/queries/can.json). Time windows are selected from observed data and restricted to one system or source segment, avoiding comparisons between unrelated relative clocks.

Before accepting query timings, the runner independently reads the NDJSON with DuckDB's JSON reader, without the CLP extension. It checks catalog row counts and every query result against that reference. Integer counts must match exactly. Floating-point aggregates allow small differences from summation order. A mismatch stops the run and marks it failed. This validates the benchmark queries, not every field of every decompressed record.

## Find the results

The [recorded benchmark report](metrics-benchmark.md) covers both full prepared datasets and includes every query, its timing and exact SQL. Its measured results are saved alongside the report.

The runner prints the report path when it finishes:

```text
.work/metrics/runs/<run>/
  report.md                      Compression and query-performance tables
  results.json                   Settings, versions, hashes and results
  westermo/                      Also can/ when selected
    archives/                    Single-file CLP archives
    catalog.duckdb               Registered catalog and typed view
    decode-cache/                Shared 64MiB on-disk cache
    compression.json             Ratio, throughput and peak RSS
    queries.json                 Per-query results and latency statistics
    query-samples.jsonl          Every measured repetition
    queries/*.sql                Exact executed SQL
    plans/*.txt                  Query plans collected outside timing
    reference-results.json       Expected answers from source NDJSON
    catalog-setup.sql            Registration and view definitions
    *.stdout, *.stderr           Preparation and compression logs
```

Use the paths in `results.json` when reopening a catalog. The archives remain separate files, so keep them in place alongside the catalog. Results and downloaded data are excluded from Git.

## Dataset preparation details

### Westermo

[prepare_westermo.py](../scripts/datasets/prepare_westermo.py) downloads the 19 CSVs, description and license from revision `47e0ccdc10c30e86327d6eb828ed48d164469fd4`. It verifies each file's size and Git blob checksum against the checked-in [source inventory](../scripts/datasets/westermo-source.json), then records SHA-256 hashes of the prepared files.

Each CSV row becomes a document with `system_id` taken from the filename. Metric-name hyphens become underscores, so `load-1m` becomes `load_1m`. Missing values are omitted, zeros are retained and integer values remain exact. `timestamp` is seconds since collection began, not Unix time. The typed view exposes it as `elapsed_s`. The downloaded source description defines measurement units and which fields are already rates.

The data is [CC BY 4.0](https://github.com/westermo/test-system-performance-dataset/blob/47e0ccdc10c30e86327d6eb828ed48d164469fd4/LICENSE). Retain its license and credit Per Erik Strandberg and Yosh Marklund, *The Westermo test system performance data set* (2023).

### Public CAN

[download_public_can.py](../scripts/datasets/download_public_can.py) downloads and verifies the required raw logs, NumPy payload arrays, DBC signal definitions and log schemas. [convert_public_can.py](../scripts/datasets/convert_public_can.py) recovers actual payload lengths from raw logs, checks decoded signals against an independent bit-field interpretation and writes wide NDJSON. The benchmark uses its `--wide-only` mode to avoid generating the much larger long-table representation.

| Source | Pinned revision |
|---|---|
| [comma2k19 data](https://huggingface.co/datasets/commaai/comma2k19) | `4bff77c7254c654c28d4c2726186b4e825adccee` |
| [opendbc](https://github.com/commaai/opendbc) | `ec2a7dcf2fb3550c30ab6be32577547e0247c7ca` |
| [cereal log schemas](https://github.com/commaai/cereal) | `ab79999e5dc2de25c3eb0c9acbed47c9e81f5d79` |

The complete preparation covers 382 distinct segments from 42 routes. It excludes send receipts and records without a matching DBC definition and payload length. About 72.72% of received frames are decoded. The manifest records skipped records and coverage, while `signal_catalog.json` records units and signal definitions. Preserve the downloaded source licenses and attribution when sharing derived data.

The `ts` field is relative device milliseconds, truncated from source seconds by the converter. Wheel speeds decoded with these DBCs use km/h, unlike the publisher's separate processed-speed arrays in m/s. `source_file` identifies a segment, and `source_index.csv` maps it to a route. Clocks can reset across routes. Neither dataset is declared an absolute timestamp during CLP compression.

To reuse complete CAN tables previously produced by this converter:

```bash
.venv/bin/python scripts/benchmark_metrics.py --dataset can \
  --can-tables /path/to/public_can_tables
```

The runner verifies the manifest and hashes before reuse. Alternatively, `--can-source /path/to/public_can_work` reuses downloaded inputs and runs conversion. A sample directory requires the matching `--can-segments` value. These options do not silently substitute a smaller dataset.
