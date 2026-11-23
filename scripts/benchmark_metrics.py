#!/usr/bin/env python3
"""Download, prepare, compress and benchmark public metrics with DuckDB catalogs."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import shlex
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
MIB = 1024 * 1024


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run_command(command, directory, label, measure=False):
    """Separate preparation and verification from the compression timer and RSS."""
    command = list(map(str, command))
    save(directory / (label + '-command.json'), command)
    print(shlex.join(command), flush=True)
    invoked = command
    if measure:
        invoked = ['/usr/bin/time', '-f',
                   '{"wall_seconds":%e,"user_seconds":%U,"system_seconds":%S,"peak_rss_kib":%M,"exit_code":%x}',
                   '-o', str(directory / (label + '-resources.json')), *command]
    with (directory / (label + '.stdout')).open('w') as out, (directory / (label + '.stderr')).open('w') as err:
        start = time.perf_counter()
        completed = subprocess.run(invoked, stdout=out, stderr=err, env={**os.environ, 'LC_ALL': 'C'})
        elapsed = time.perf_counter() - start
    if completed.returncode:
        raise RuntimeError(f'{label} failed ({completed.returncode}); see {directory / (label + ".stderr")}')
    result = {'wall_seconds': elapsed}
    if measure:
        result.update(json.loads((directory / (label + '-resources.json')).read_text()))
        result['wall_seconds'] = elapsed  # Higher resolution than GNU time's text output.
    return result


def prepare(dataset, args, directory):
    if dataset == 'westermo':
        base = args.work_dir / ('westermo' if args.westermo_systems == 19 else f'westermo-{args.westermo_systems}')
        tables = base / 'tables-v1'
        run_command([sys.executable, SCRIPTS / 'datasets/prepare_westermo.py',
                     '--source', base / 'source', '--output', tables,
                     '--systems', args.westermo_systems, '--workers', args.workers], directory, 'prepare')
    else:
        base = args.work_dir / ('can' if not args.can_segments else f'can-{args.can_segments}')
        tables = args.can_tables or base / 'tables'
        if not args.can_tables:
            source = args.can_source or base / 'source'
            downloader = SCRIPTS / 'datasets/download_public_can.py'
            if args.can_source:
                command = [sys.executable, downloader, 'verify', '--work-dir', source]
            else:
                command = [sys.executable, downloader, '--work-dir', source,
                           '--workers', args.workers, '--max-segments', args.can_segments]
            run_command(command, directory, 'download')
            if not tables.exists():
                run_command([sys.executable, SCRIPTS / 'datasets/convert_public_can.py',
                             '--work-dir', source, '--output', tables, '--wide-only',
                             '--workers', args.workers, '--max-segments', args.can_segments], directory, 'convert')
    manifest_path = tables / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if not manifest.get('complete'):
        raise ValueError(f'Incomplete preparation: {manifest_path}')
    if dataset == 'can':
        if (manifest.get('dataset_revision') != '4bff77c7254c654c28d4c2726186b4e825adccee'
                or not manifest.get('every_signal_independently_checked')
                or not manifest.get('raw_occurrences_fully_matched')):
            raise ValueError('CAN input must be produced by the pinned, validated converter')
        expected_segments = args.can_segments or 382
        if manifest.get('segments') != expected_segments:
            raise ValueError(f'Expected {expected_segments} CAN segments, found {manifest.get("segments")}; '
                             'use --can-segments to explicitly select a smaller dataset')
    items = [item for item in manifest['files'] if item['path'].endswith('.ndjson')
             and (dataset != 'can' or item['path'].startswith('wide/CAN/'))]
    if not items:
        raise ValueError('Preparation manifest contains no benchmark NDJSON')
    paths = []
    for item in items:
        path = (tables / item['path']).resolve()
        if not path.is_relative_to(tables.resolve()) or '\n' in str(path) or '\r' in str(path):
            raise ValueError(f'Invalid input path: {path}')
        if path.stat().st_size != item['bytes'] or sha256(path) != item['sha256']:
            raise ValueError(f'Input changed: {path}')
        paths.append(path)
    save(directory / 'input-manifest.json', manifest)
    return paths, {'files': len(items), 'bytes': sum(item['bytes'] for item in items),
                   'records': sum(item['rows'] for item in items), 'tables': str(tables),
                   'manifest_sha256': sha256(manifest_path),
                   'scope': f"{manifest['segments']} CAN segments" if dataset == 'can'
                            else f"{manifest['systems']} Westermo systems"}


def compress(paths, inputs, args, directory):
    listing = directory / 'inputs.txt'
    listing.write_text(''.join(str(path) + '\n' for path in paths))
    archives = directory / 'archives'
    command = [args.cli, 'c', '--single-file-archive', '--print-archive-stats',
               '--compression-level', args.compression_level,
               '--dictionary-compression-level', args.dictionary_level,
               '--target-encoded-size', args.target_encoded_mib * MIB,
               '--files-from', listing, archives]
    measurement = run_command(command, directory, 'compress', measure=True)
    stats = [json.loads(line) for line in (directory / 'compress.stdout').read_text().splitlines() if line.strip()]
    files = sorted(archives.iterdir())
    if not files or any(not path.is_file() for path in files):
        raise ValueError('Expected single-file CLP archives')
    if len(stats) != len(files) or {item['id'] for item in stats} != {path.name for path in files}:
        raise ValueError('Archive inventory does not match writer statistics')
    total = sum(path.stat().st_size for path in files)
    if total != sum(item['size'] for item in stats) or inputs['bytes'] != sum(item['uncompressed_size'] for item in stats):
        raise ValueError('Compressed/source byte totals do not match writer statistics')
    measurement.update(archive_bytes=total, archive_count=len(files), input_bytes=inputs['bytes'],
                       ratio=inputs['bytes'] / total,
                       input_mib_per_second=inputs['bytes'] / MIB / measurement['wall_seconds'],
                       archives=[{'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha256(path)} for path in files])
    save(directory / 'compression.json', measurement)
    print(f"Compression: {inputs['bytes']:,} → {total:,} bytes ({measurement['ratio']:.2f}×), "
          f"{measurement['wall_seconds']:.2f}s, peak RSS {measurement['peak_rss_kib']/1024:.1f}MiB", flush=True)
    return archives, measurement


def render(sql, table, parameters):
    for name, value in {'table': table, **parameters}.items():
        sql = sql.replace('{{' + name + '}}', value)
    if '{{' in sql:
        raise ValueError(f'Unresolved SQL placeholder: {sql}')
    return sql


def check_result(actual, expected):
    if len(actual) != len(expected):
        raise ValueError(f'Result row count differs: {len(actual)} != {len(expected)}')
    for row, reference in zip(actual, expected):
        if len(row) != len(reference):
            raise ValueError('Result column count differs')
        for value, wanted in zip(row, reference):
            if type(wanted) is float:
                correct = type(value) in (float, int) and math.isclose(value, wanted, rel_tol=1e-9, abs_tol=1e-6)
            else:
                correct = type(value) is type(wanted) and value == wanted
            if not correct:
                raise ValueError(f'Result differs: {value!r} != {wanted!r}')


def references(duckdb, paths, suite, inputs, args, directory):
    """Use DuckDB's own JSON reader, without loading CLP, as the independent reference."""
    with duckdb.connect(str(directory / 'reference.duckdb')) as connection:
        connection.execute(f'SET threads={args.threads}')
        connection.execute(f'SET memory_limit={literal(args.memory_limit)}')
        types = ','.join(literal(col['document_path']) + ':' + literal(col['sql_type']) for col in suite['columns'])
        projection = ','.join(identifier(col['document_path']) + ' AS ' + identifier(col['column_name'])
                              for col in suite['columns'])
        file_list = '[' + ','.join(literal(path) for path in paths) + ']'
        sql = f"CREATE TABLE source AS SELECT {projection} FROM read_json({file_list}, format='newline_delimited', columns={{{types}}})"
        (directory / 'reference-setup.sql').write_text(sql + ';\n')
        connection.execute(sql)
        count = connection.execute('SELECT count(*) FROM source').fetchone()[0]
        if count != inputs['records']:
            raise ValueError('Source NDJSON row count differs from the conversion manifest')
        window = suite['window']
        source, clock, signal = [identifier(window[key]) for key in ('source_column', 'time_column', 'signal_column')]
        row = connection.execute(f'SELECT {source}, min({clock}) FROM source WHERE {signal} IS NOT NULL '
                                 f'GROUP BY {source} ORDER BY {source} LIMIT 1').fetchone()
        if row is None or row[1] is None:
            raise ValueError('Dataset has no samples for the time-window queries')
        parameters = {'window_source': literal(row[0]), 'window_start': str(row[1]),
                      'window_end': str(row[1] + window['width'])}
        expected = {}
        for query in suite['queries']:
            sql = render(query['sql'], 'source', parameters)
            cursor = connection.execute(sql)
            expected[query['id']] = {'columns': [col[0] for col in cursor.description], 'rows': cursor.fetchall()}
    save(directory / 'reference-results.json', expected)
    save(directory / 'query-parameters.json', parameters)
    return parameters, expected


def cache_info(connection):
    cursor = connection.execute('SELECT * FROM clp_decode_cache_info()')
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, cursor.fetchone()))


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def benchmark_queries(duckdb, archives, suite, parameters, expected, inputs, args, directory):
    connection = duckdb.connect(str(directory / 'catalog.duckdb'), config={'allow_unsigned_extensions': 'true'})
    startup = time.perf_counter()
    catalog, view = suite['catalog'], suite['view']
    table = identifier(catalog) + '.' + identifier(view)
    columns = '[' + ','.join('{' + ','.join(literal(key) + ':' + literal(value) for key, value in col.items()) + '}'
                            for col in suite['columns']) + ']'
    # Isolate datasets while inheriting the extension's cache defaults.
    cache = directory / 'decode-cache'
    setup = [f'LOAD {literal(args.extension)}', f'SET threads={args.threads}',
             f'SET memory_limit={literal(args.memory_limit)}',
             f'SET clp_decode_cache_dir={literal(cache)}',
             f'CALL clp_create_catalog({literal(catalog)})',
             f'CALL clp_register({literal(catalog)}, {literal(archives)})',
             f'CALL clp_create_mapped_view({literal(catalog)}, {literal(view)}, columns := {columns})']
    (directory / 'catalog-setup.sql').write_text(';\n'.join(setup) + ';\n')
    samples = {query['id']: [] for query in suite['queries']}
    try:
        for sql in setup:
            connection.execute(sql).fetchall()
        setup_seconds = time.perf_counter() - startup
        if connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] != inputs['records']:
            raise ValueError('CLP catalog row count differs from source NDJSON')
        initial = cache_info(connection)
        cache_enabled = connection.execute("SELECT current_setting('clp_decode_cache_enabled')").fetchone()[0]
        entry_bytes = connection.execute("SELECT current_setting('clp_decode_cache_entry_bytes')").fetchone()[0]
        if not cache_enabled or initial['capacity_bytes'] <= 0 or entry_bytes <= 0:
            raise ValueError('Query measurements require an enabled decode cache with positive capacity and entry limit')
        queries = [{**query, 'sql': render(query['sql'], table, parameters)} for query in suite['queries']]
        sql_dir = directory / 'queries'
        sql_dir.mkdir()
        for query in queries:
            (sql_dir / (query['id'] + '.sql')).write_text(query['sql'] + ';\n')
        # Run untimed workload passes before measuring; keep one shared cache throughout.
        for warmup in range(args.warmups):
            print(f'{catalog}: warming workload, pass {warmup + 1}/{args.warmups}', flush=True)
            for query in queries:
                try:
                    check_result(connection.execute(query['sql']).fetchall(), expected[query['id']]['rows'])
                except Exception as error:
                    raise RuntimeError(f"{query['id']} failed during warm-up: {error}") from error
        before = cache_info(connection)
        with (directory / 'query-samples.jsonl').open('w') as output:
            for repetition in range(args.repeats):
                offset = repetition % len(queries)
                order = queries[offset:] + queries[:offset]
                if repetition % 2:
                    order = list(reversed(order))
                for query in order:
                    start = time.perf_counter_ns()
                    cursor = connection.execute(query['sql'])
                    rows = cursor.fetchall()
                    elapsed = (time.perf_counter_ns() - start) / 1e6
                    try:
                        check_result(rows, expected[query['id']]['rows'])
                    except ValueError as error:
                        raise ValueError(f"{query['id']}: {error}") from error
                    if [col[0] for col in cursor.description] != expected[query['id']]['columns']:
                        raise ValueError(f"Result columns differ: {query['id']}")
                    samples[query['id']].append(elapsed)
                    output.write(json.dumps({'query': query['id'], 'repetition': repetition + 1,
                                             'milliseconds': elapsed, 'validated': True}) + '\n')
                    output.flush()
                print(f'{catalog}: query pass {repetition + 1}/{args.repeats}', flush=True)
        after = cache_info(connection)
        if after['capacity_bytes'] != initial['capacity_bytes'] or after['used_bytes'] > after['capacity_bytes']:
            raise ValueError('Decode cache budget verification failed')
        # Keep plans and final results outside the measured interval.
        plans = directory / 'plans'
        plans.mkdir()
        for query in queries:
            plan = connection.execute('EXPLAIN ANALYZE ' + query['sql']).fetchall()
            (plans / (query['id'] + '.txt')).write_text('\n'.join(str(row[-1]) for row in plan) + '\n')
    finally:
        connection.close()
    result = {'setup_seconds': setup_seconds, 'cache_before_measurement': before, 'cache_after_measurement': after,
              'cache_enabled': cache_enabled, 'cache_entry_limit_bytes': entry_bytes, 'queries': []}
    for query in queries:
        values = samples[query['id']]
        result['queries'].append({**query, 'samples_ms': values, 'average_ms': statistics.mean(values),
                                  'p50_ms': statistics.median(values), 'p95_ms': percentile(values, .95),
                                  'result': expected[query['id']], 'validated': True})
    save(directory / 'queries.json', result)
    return result


def write_report(path, report):
    lines = ['# Public metrics benchmark', '',
             'Query measurements use catalog typed views. '
             'All query results matched independent queries over the source NDJSON.', '',
             f"{report['settings']['warmups']} complete warm-up passes precede {report['settings']['repeats']} measured passes.", '',
             '## Compression', '',
             '| Dataset | Scope | Records | NDJSON | CLP archives | Ratio | Time | Input throughput | Peak RSS |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for result in report['datasets']:
        info, comp = result['input'], result['compression']
        lines.append(f"| {result['label']} | {info['scope']} | {info['records']:,} | {info['bytes']/MIB:.1f}MiB | "
                     f"{comp['archive_bytes']/MIB:.1f}MiB | {comp['ratio']:.2f}× | {comp['wall_seconds']:.2f}s | "
                     f"{comp['input_mib_per_second']:.1f}MiB/s | {comp['peak_rss_kib']/1024:.1f}MiB |")
    lines += ['', 'Ratio = prepared NDJSON bytes / all CLP archive bytes. It is not a ratio against the source ZIPs, '
              'CSV or binary CAN payloads. Compression timings and peak RSS cover the CLI process, excluding download, '
              'conversion, hashing, reference computation and queries.', '', '## Query performance', '',
              '| Dataset | Query | Operation | Average | P50 | P95 |', '|---|---|---|---:|---:|---:|']
    for result in report['datasets']:
        for query in result['query']['queries']:
            lines.append(f"| {result['label']} | {query['id']} | {query['label']} | {query['average_ms']:.2f}ms | "
                         f"{query['p50_ms']:.2f}ms | {query['p95_ms']:.2f}ms |")
    lines += ['', 'Each row summarizes repetitions of that query. Timing includes SQL planning, execution and complete '
              'result delivery through the DuckDB Python client. Startup, extension loading, catalog registration, '
              'warm-up, validation and report writing are excluded. SQL is submitted afresh for each repetition.']
    lines += ['', 'CAN product and ratio queries exercise arithmetic aggregation, not physical energy calculations. '
              'Time-window queries use one source segment/system and preserve the relative clock.', '',
              'See `results.json` for settings and binary identities. Each dataset directory contains compression logs, '
              'a persistent `catalog.duckdb`, the exact SQL, reference results, individual query timings and query plans.', '']
    path.write_text('\n'.join(lines))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', choices=['all', 'westermo', 'can'], default='all')
    parser.add_argument('--work-dir', type=Path, default=ROOT / '.work/metrics')
    parser.add_argument('--output', type=Path, help='New results directory; never overwritten')
    parser.add_argument('--cli', type=Path, default=ROOT / 'bin/x86_64/clp-s')
    parser.add_argument('--extension', type=Path, default=ROOT / 'bin/x86_64/clp_s.duckdb_extension')
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--warmups', type=int, default=2)
    parser.add_argument('--threads', type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument('--workers', type=int, default=4, help='Download/conversion workers; measurements are sequential')
    parser.add_argument('--memory-limit', default='4GB')
    parser.add_argument('--compression-level', type=int, default=3)
    parser.add_argument('--dictionary-level', type=int, default=9)
    parser.add_argument('--target-encoded-mib', type=int, default=512)
    parser.add_argument('--westermo-systems', type=int, choices=range(1, 20), default=19)
    parser.add_argument('--can-segments', type=int, default=0, help='0 = complete RAV4 subset; positive values are explicitly labelled samples')
    reuse = parser.add_mutually_exclusive_group()
    reuse.add_argument('--can-source', type=Path, help='Reuse downloaded inputs from download_public_can.py')
    reuse.add_argument('--can-tables', type=Path, help='Reuse validated convert_public_can.py output instead of downloading/converting again')
    args = parser.parse_args()
    if min(args.repeats, args.warmups, args.threads, args.workers, args.target_encoded_mib) < 1 or args.can_segments < 0:
        parser.error('Repetitions, warm-ups, threads, workers and sizes must be positive; segment count must be nonnegative')
    if args.can_segments > 382:
        parser.error('The pinned RAV4 subset contains at most 382 distinct segments')
    for name in ['work_dir', 'cli', 'extension', 'output', 'can_source', 'can_tables']:
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    return args


def main():
    args = parse_args()
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError('Install scripts/requirements-metrics.txt before running this command') from error
    if not Path('/usr/bin/time').is_file():
        raise RuntimeError('Install GNU time (/usr/bin/time) to measure compression peak RSS')
    build = json.loads((ROOT / 'bin/x86_64/BUILD_INFO.json').read_text())
    if duckdb.__version__ != build['duckdb_version'].removeprefix('v'):
        raise RuntimeError(f"DuckDB {duckdb.__version__} does not match the supplied extension ({build['duckdb_version']})")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    # Prevent this runner's downloads/conversion and measurements from competing with another run.
    with (args.work_dir / '.benchmark.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another metrics benchmark is using this work directory') from error
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        output = args.output or args.work_dir / 'runs' / stamp
        output.mkdir(parents=True, exist_ok=False)
        report = {'status': 'running', 'started_at_utc': stamp,
                  'settings': {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
                  'environment': {'platform': platform.platform(), 'python': sys.version, 'duckdb': duckdb.__version__,
                                  'cpu_count': os.cpu_count(), 'load_average': os.getloadavg()}, 'datasets': []}
        report['code_sha256'] = {str(path.relative_to(ROOT)): sha256(path)
                                 for path in sorted(SCRIPTS.rglob('*')) if path.suffix in ('.py', '.json', '.txt')}
        report['environment']['packages'] = {name: importlib.metadata.version(name)
                                              for name in ['duckdb', 'numpy', 'cantools', 'pycapnp']}
        save(output / 'results.json', report)
        try:
            version = subprocess.check_output([str(args.cli), '--version'], text=True).strip()
            report['binaries'] = {'cli_version': version, 'cli_sha256': sha256(args.cli),
                                  'extension_sha256': sha256(args.extension)}
            # Load the extension before large downloads so ABI/expiry problems fail early.
            with duckdb.connect(str(output / 'preflight.duckdb'), config={'allow_unsigned_extensions': 'true'}) as test:
                test.execute(f'LOAD {literal(args.extension)}')
                test.execute(f'SET memory_limit={literal(args.memory_limit)}')
            for dataset in (['westermo', 'can'] if args.dataset == 'all' else [args.dataset]):
                directory = output / dataset
                directory.mkdir()
                paths, inputs = prepare(dataset, args, directory)
                archives, compressed = compress(paths, inputs, args, directory)
                suite = json.loads((SCRIPTS / 'queries' / (dataset + '.json')).read_text())
                save(directory / 'suite.json', suite)
                print(f'{dataset}: computing independent reference results...', flush=True)
                parameters, expected = references(duckdb, paths, suite, inputs, args, directory)
                queried = benchmark_queries(duckdb, archives, suite, parameters, expected, inputs, args, directory)
                report['datasets'].append({'dataset': dataset, 'label': suite['label'], 'input': inputs,
                                           'compression': compressed, 'query': queried})
                save(output / 'results.json', report)
            report['status'] = 'passed'
            write_report(output / 'report.md', report)
        except BaseException as error:
            report.update(status='failed', error=str(error))
            raise
        finally:
            save(output / 'results.json', report)
        print(f'PASS: {output / "report.md"}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
