#!/usr/bin/env python3
"""Validate schema, string-shape and field-history queries on a completed metrics run.

Uses the complete original JSONL files as an independent reference. Existing
archives are reused. No downloads, compression or existing catalog edits occur.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import threading
import time

import duckdb

ROOT = Path(__file__).resolve().parents[1]


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')


def inventory(layouts):
    result = defaultdict(set)
    types = {'UBIGINT': 'integer', 'BIGINT': 'integer', 'DOUBLE': 'float',
             'VARCHAR': 'string', 'BOOLEAN': 'boolean', 'NULL': 'null'}
    for layout in layouts:
        # These two prepared datasets contain flat scalar documents. Refuse to
        # silently approximate an unexpected nested or unresolved source type.
        for path, kind in json.loads(layout).items():
            if kind not in types:
                raise ValueError(f'Unexpected reference type: {path}: {kind}')
            result[path].add(types[kind])
    return [(path, sorted(kinds)) for path, kinds in sorted(result.items())]


def tree_from_fields(fields):
    return {'types': ['object'], 'children': {
        name: {'types': types, 'children': {}} for name, types in fields}}


def execute(connection, sql, timeout=60):
    timer = threading.Timer(timeout, connection.interrupt)
    timer.start()
    try:
        start = time.perf_counter_ns()
        rows = connection.execute(sql).fetchall()
        elapsed = (time.perf_counter_ns() - start) / 1e6
        return rows, elapsed
    finally:
        timer.cancel()
        timer.join()


def source_profiles(paths, suite, params, directory, threads):
    """Read every record once, retaining small per-source/schema reference groups."""
    files = '[' + ','.join(quote(p) for p in paths) + ']'
    clock = 'timestamp' if suite['catalog'] == 'westermo' else 'ts'
    source = suite['window']['source_column']
    signal = suite['window']['signal_column']
    auxiliary = 'load_15m' if suite['catalog'] == 'westermo' else 'WHEEL_SPEED_FL'
    threshold = 2 if suite['catalog'] == 'westermo' else 100
    window = f"clock >= {params['window_start']} AND clock < {params['window_end']} AND signal IS NOT NULL"
    selections = {'all': 'TRUE', 'value': f'signal >= {threshold}', 'window': window,
                  'derived': 'signal > auxiliary'}
    aggregates = []
    for name, condition in selections.items():
        for aggregate in ['count(*)', 'min(clock)', 'max(clock)']:
            aggregates.append(f'{aggregate} FILTER (WHERE {condition})')
    sql = f"""WITH samples AS (
 SELECT json_structure(json)::VARCHAR AS layout,
        json_extract_string(json, '$.{source}') AS source,
        json_extract_string(json, '$.{clock}')::BIGINT AS clock,
        json_extract_string(json, '$.{signal}')::DOUBLE AS signal,
        json_extract_string(json, '$.{auxiliary}')::DOUBLE AS auxiliary
 FROM read_ndjson_objects({files})
)
SELECT layout, source, {', '.join(aggregates)}
FROM samples GROUP BY layout, source ORDER BY layout, source"""
    (directory / 'reference.sql').write_text(sql + ';\n')
    with duckdb.connect() as ref:
        ref.execute(f'SET threads={threads}')
        ref.execute("SET memory_limit='8GiB'")
        rows, elapsed = execute(ref, sql, timeout=900)
    profiles = []
    for layout, label, *stats in rows:
        profile = {'layout': layout, 'source': label}
        for index, name in enumerate(selections):
            profile[name] = dict(zip(['count', 'first', 'last'], stats[index*3:index*3+3]))
        profiles.append(profile)
    save(directory / 'reference-profiles.json', profiles)
    print(f"Reference: {sum(p['all']['count'] for p in profiles):,} records in {elapsed/1000:.2f}s", flush=True)
    return profiles, {'clock': clock, 'source': source, 'signal': signal,
                      'auxiliary': auxiliary, 'threshold': threshold}


def verify_dataset(name, args):
    original = args.run / name
    output = args.output / name
    output.mkdir()
    paths = [Path(p) for p in (original / 'inputs.txt').read_text().splitlines() if p]
    archives = sorted((original / 'archives').iterdir())
    if not archives or any(not p.is_file() for p in archives):
        raise ValueError('Expected existing single-file archives')
    suite = json.loads((ROOT / 'scripts/queries' / f'{name}.json').read_text())
    params = json.loads((original / 'query-parameters.json').read_text())
    profiles, config = source_profiles(paths, suite, params, output, args.threads)
    clock, source, signal = [config[k] for k in ['clock', 'source', 'signal']]
    threshold = config['threshold']
    label = params['window_source'][1:-1].replace("''", "'")
    scoped = [p for p in profiles if p['source'] == label]
    conditions = {
        'all': '',
        'value': f'WHERE _events.{signal}::DOUBLE >= {threshold}',
        'window': (f"WHERE _events.{source}::VARCHAR={quote(label)} AND _events.{signal}::DOUBLE IS NOT NULL "
                   f"AND _events.{clock}::BIGINT >= {params['window_start']} "
                   f"AND _events.{clock}::BIGINT < {params['window_end']}"),
        'derived': f'WHERE _events.{signal}::DOUBLE > _events.{config["auxiliary"]}::DOUBLE',
    }
    expected_fields = {selection: inventory(p['layout'] for p in (scoped if selection=='window' else profiles)
                                          if p[selection]['count']) for selection in conditions}
    archive_list = '[' + ','.join(quote(p) for p in archives) + ']'
    direct = f'clp_search({archive_list})'
    catalog = f'{name}.events'
    cases = []

    def add(key, operation, sql, expected, direct_sql=None):
        cases.append({'id': key, 'operation': operation, 'sql': sql,
                      'direct_sql': direct_sql or sql.replace(catalog, direct), 'expected': expected})

    for selection, condition in conditions.items():
        add('schema_' + selection, 'Schema fields: ' + selection,
            f'SELECT clp_schema() FROM {catalog} {condition} ORDER BY field_path', expected_fields[selection])
        add('tree_' + selection, 'Schema tree: ' + selection,
            f'SELECT clp_schema_tree() AS schema_tree FROM {catalog} {condition}',
            [(tree_from_fields(expected_fields[selection]),)])
    prefix = 'cpu_' if name == 'westermo' else 'WHEEL_SPEED_'
    add('schema_paths', 'Filter the returned field paths',
        f"SELECT * FROM (SELECT clp_schema() FROM {catalog}) f WHERE starts_with(field_path,{quote(prefix)}) ORDER BY field_path",
        [r for r in expected_fields['all'] if r[0].startswith(prefix)])
    add('schema_empty', 'Schema of an empty selection',
        f'SELECT clp_schema() FROM {catalog} WHERE _events.{signal}::DOUBLE > 1e100', [])

    def field_history(selection):
        result = {}
        for p in scoped:
            if not p[selection]['count']:
                continue
            for field in json.loads(p['layout']):
                old = result.get(field)
                value = p[selection]
                if old:
                    result[field] = (min(old[0],value['first']),max(old[1],value['last']),old[2]+value['count'])
                else:
                    result[field] = (value['first'],value['last'],value['count'])
        return result

    history = field_history('all')
    predicate = f'_events.{source}::VARCHAR={quote(label)}'
    common = f'time_path={quote(clock)}, start_value=0, predicate={quote(predicate)}'
    inventory_source = f'clp_dataset_fields({quote(name)}, {common})'
    direct_inventory = f'clp_archive_fields({archive_list}, {common})'
    args_sql = f'_events.{signal}, _events.{clock}::BIGINT'
    for key, indices, cols in [('first',[0],'first_seen'),('last',[1],'last_seen'),('both',[0,1],'first_seen,last_seen')]:
        result = [tuple(history[signal][i] for i in indices)]
        projections = ', '.join(f'clp_{bound}({args_sql}) AS {bound}' for bound in cols.split(','))
        q = f'SELECT {projections} FROM {catalog} WHERE {predicate}'
        add('history_' + key, 'Field history in original units: ' + key, q, result)
    q=f'SELECT field_path,first_seen,last_seen,matching_rows FROM {inventory_source} ORDER BY field_path'
    add('history_inventory','First/last seen for all fields',q,
        [(field,*bounds) for field,bounds in sorted(history.items())],q.replace(inventory_source,direct_inventory))
    selected = field_history('value').get(signal)
    value_predicate=predicate+f' AND _events.{signal}::DOUBLE >= {threshold}'
    q = (f'SELECT clp_first_seen({args_sql}) AS first_seen, clp_last_seen({args_sql}) AS last_seen '
         f'FROM {catalog} WHERE {value_predicate}')
    add('history_filtered', 'Field history with a value predicate', q,
        [selected[:2] if selected else (None, None)])
    # Compare the former explicit conversion with native numeric ordering.
    # This is diagnostic SQL, not an assertion of Unix epoch time.
    factor = 1000 if name == 'westermo' else 1
    args_sql=f'_events.{signal}, epoch_ms(_events.{clock}::BIGINT * {factor})'
    q=(f'SELECT epoch_ms(clp_first_seen({args_sql})) // {factor} AS first_seen,'
       f'epoch_ms(clp_last_seen({args_sql})) // {factor} AS last_seen '
       f'FROM {catalog} WHERE {predicate}')
    add('history_aggregate_explicit','First/last aggregate with explicit clock conversion',q,[history[signal][:2]])
    q=f'SELECT clp_first_seen(_events.{signal},_events.{clock}),clp_last_seen(_events.{signal},_events.{clock}) FROM {catalog} WHERE {predicate}'
    add('history_raw_clock','Raw relative clock is not a declared timestamp',q,[(None,None)])

    for selection in ['all','value']:
        groups=defaultdict(int)
        for p in profiles:
            groups[p['source']] += p[selection]['count']
        result=sorted(((label,n) for label,n in groups.items() if n),key=lambda x:(-x[1],x[0]))
        add('shapes_' + selection,'String-shape counts: ' + selection,
            f'SELECT clp_string_shape(_events.{source}::VARCHAR) AS shape,count(*) AS n FROM {catalog} '
            f'{conditions[selection]} GROUP BY shape ORDER BY n DESC,shape',result)

    results=[]
    with duckdb.connect(str(output/'catalog.duckdb'),config={'allow_unsigned_extensions':True}) as c:
        c.execute(f'LOAD {quote(args.extension)}')
        c.execute(f'SET threads={args.threads}')
        c.execute("SET memory_limit='8GiB'")
        c.execute(f'SET clp_decode_cache_dir={quote(output / "decode") }')
        c.execute(f'CALL clp_create_catalog({quote(name)})')
        c.execute(f'CALL clp_register({quote(name)},{archive_list})')
        cols='['+','.join('{'+','.join(quote(k)+':'+quote(v) for k,v in col.items())+'}' for col in suite['columns'])+']'
        c.execute(f'CALL clp_create_mapped_view({quote(name)},{quote(suite["view"])},columns={cols})')
        assert c.execute(f'SELECT count(*) FROM {catalog}').fetchone()[0]==sum(p['all']['count'] for p in profiles)
        for case in cases:
            print(name, case['id'], flush=True)
            expected=case.pop('expected')
            for route in ['sql','direct_sql']:
                actual,_=execute(c,case[route])
                assert actual==expected,(name,case['id'],route,actual,expected)
            (output/(case['id']+'.sql')).write_text(case['sql']+';\n')
            case['result']=expected
            case['validated_catalog_and_direct']=True
            for _ in range(args.warmups):
                actual,_=execute(c,case['sql'])
                assert actual==expected
            durations=[]
            for _ in range(args.repeats):
                actual,elapsed=execute(c,case['sql'])
                assert actual==expected
                durations.append(elapsed)
            case['milliseconds']=durations
            case['p50_ms']=statistics.median(durations)
            case['average_ms']=statistics.mean(durations)
            case['plan']=c.execute('EXPLAIN '+case['sql']).fetchall()[0][-1]
            if case['id'] in ('history_first', 'history_last', 'history_both', 'history_filtered'):
                assert 'clp_field_history' in case['plan'].lower(), case['plan']
            results.append(case)
            save(output/'queries.json',results)
            print(f"  {len(actual)} result rows; P50 {case['p50_ms']:.2f}ms",flush=True)
        # The view omits most fields. Discovery must still report the complete
        # schema of the underlying documents selected by its renamed columns.
        typed_clock=suite['window']['time_column']
        typed_source=suite['window']['source_column']
        typed_condition=(f'{typed_source}={quote(label)} AND {signal} IS NOT NULL '
                         f'AND {typed_clock}>={params["window_start"]} AND {typed_clock}<{params["window_end"]}')
        typed_sql=f'SELECT clp_schema() FROM {name}.{suite["view"]} WHERE {typed_condition} ORDER BY field_path'
        assert execute(c,typed_sql)[0]==expected_fields['window']
        (output/'schema_typed.sql').write_text(typed_sql+';\n')
        bound_sql=f'SELECT clp_schema() FROM {catalog} WHERE _events.{source}::VARCHAR=? AND _events.{signal}::DOUBLE>=? ORDER BY field_path'
        scoped_value=inventory(p['layout'] for p in scoped if p['value']['count'])
        assert c.execute(bound_sql,[label,threshold]).fetchall()==scoped_value
        assert c.execute(bound_sql,['missing-source',threshold]).fetchall()==[]
    return {'name':name,'records':sum(p['all']['count'] for p in profiles),'files':len(paths),
            'archives':len(archives),'fields':len(expected_fields['all']),'source':label,
            'clock':clock,'clock_unit':'s' if name=='westermo' else 'ms',
            'signal':signal,'history':history[signal],'queries':results,'typed_view_and_parameters_validated':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True,help='Completed benchmark_metrics.py run directory')
    parser.add_argument('--output',type=Path,default=ROOT/'.work/discovery'/datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--extension',type=Path,default=ROOT/'bin/x86_64/clp_s.duckdb_extension')
    parser.add_argument('--threads',type=int,default=4)
    parser.add_argument('--warmups',type=int,default=2)
    parser.add_argument('--repeats',type=int,default=7)
    args=parser.parse_args()
    if args.threads<1 or args.repeats<1 or args.warmups<0:
        parser.error('threads/repeats must be positive and warmups nonnegative')
    args.run=args.run.resolve(); args.output=args.output.resolve(); args.extension=args.extension.resolve()
    args.output.mkdir(parents=True,exist_ok=False)
    os.environ['CLP_CATALOG_DATABASE']=':memory:'
    with args.extension.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
    report={'created_at':datetime.now(timezone.utc).isoformat(),'duckdb':duckdb.__version__,
            'extension_sha256':digest,'input_run':str(args.run),'warmups':args.warmups,'repeats':args.repeats,
            'threads':args.threads,'datasets':[]}
    for dataset in ['westermo','can']:
        report['datasets'].append(verify_dataset(dataset,args))
        save(args.output/'results.json',report)
    print('PASS: both complete datasets, catalog/direct parity, typed views and bound parameters',flush=True)
    print(args.output,flush=True)


if __name__=='__main__':
    main()
