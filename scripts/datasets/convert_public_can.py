#!/usr/bin/env python3
"""Decode pinned comma2k19 CAN inputs to validated long and sparse wide NDJSON.

Install scripts/requirements-metrics.txt. Download inputs with download_public_can.py.
Conversion never overwrites an output directory. The verify command uses stdlib only.
"""
import argparse
import bz2
from collections import Counter, defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
DBCS = {0: 'toyota_rav4_2017_pt_generated.dbc', 1: 'toyota_adas.dbc'}


def dump(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def sparse_wide(values):
    """Omit only nulls: zero, false, and empty strings are meaningful values."""
    return {key: value for key, value in values.items() if value is not None}


def bit_value(payload, signal):
    """Independent DBC bit extraction, separate from cantools/bitstruct."""
    if signal.is_float:
        raise ValueError('Floating-point raw CAN signals are not supported by this dataset converter')
    if signal.byte_order == 'little_endian':
        raw = (int.from_bytes(payload, 'little') >> signal.start) & ((1 << signal.length) - 1)
    else:
        raw, pos = 0, signal.start
        for _ in range(signal.length):
            raw = (raw << 1) | ((payload[pos // 8] >> (pos % 8)) & 1)
            pos = pos + 15 if pos % 8 == 0 else pos - 1
    if signal.is_signed and raw & (1 << (signal.length - 1)):
        raw -= 1 << signal.length
    return raw * signal.scale + signal.offset


def value_type(signal):
    if signal.is_float or not float(signal.scale).is_integer() or not float(signal.offset).is_integer():
        return 10
    lo = -(1 << (signal.length - 1)) if signal.is_signed else 0
    hi = (1 << (signal.length - int(signal.is_signed))) - 1
    lo, hi = sorted((lo * int(signal.scale) + int(signal.offset), hi * int(signal.scale) + int(signal.offset)))
    for i, width in enumerate((8, 16, 32, 64)):
        if lo >= 0 and hi < (1 << width):
            return 5 + i
        if lo < 0 and lo >= -(1 << (width - 1)) and hi < (1 << (width - 1)):
            return 1 + i
    raise ValueError(f'Physical integer range exceeds 64 bits: {signal.name}')


def load_databases(work):
    import cantools
    dbs = {ch: cantools.database.load_file(work / 'sources/opendbc' / name, sort_signals=None)
           for ch, name in DBCS.items()}
    for db in dbs.values():
        for msg in db.messages:
            if msg.is_multiplexed() or any(s.is_float for s in msg.signals):
                raise ValueError('Pinned dataset expects non-multiplexed integer CAN signals')
    return dbs


def check_file(path, expected_rows=None, wide=False):
    h, rows = hashlib.sha256(), 0
    with path.open('rb') as f:
        for line in f:
            obj = json.loads(line)
            if wide and any(value is None for value in obj.values()):
                raise ValueError(f'Null field in wide output: {path}:{rows + 1}')
            rows += 1
            h.update(line)
    if expected_rows is not None and rows != expected_rows:
        raise ValueError(f'Row count mismatch in {path}: {rows} != {expected_rows}')
    return {'bytes': path.stat().st_size, 'rows': rows, 'sha256': h.hexdigest()}


def init_worker(work):
    global DBS, SCHEMA
    import capnp
    DBS = load_databases(Path(work))
    SCHEMA = capnp.load(str((Path(work) / 'sources/cereal/log.capnp').resolve()))


def convert_segment(task):
    import numpy as np
    work, output, segment, source, compact, wide_only, records = task
    work, output = Path(work), Path(output)
    for record in records:
        path = work / record['path']
        if path.stat().st_size != record['bytes'] or digest(path) != record['sha256']:
            raise ValueError(f'Source integrity mismatch: {path}')
    base = work / 'segments' / segment
    arrays = {name: np.load(base / 'processed_log/CAN/raw_can' / name, allow_pickle=False)
              for name in ('address', 'src', 't', 'data')}
    n = len(arrays['t'])
    expected = {'address': '<i8', 'src': '<i8', 't': '<f8', 'data': '|S8'}
    if any(a.ndim != 1 or len(a) != n or a.dtype.str != expected[k] for k, a in arrays.items()):
        raise ValueError(f'Unexpected array layout: {segment}')
    # Preserve trailing zero bytes; bytes(np.bytes_) would truncate them.
    payloads = arrays['data'].view('V8')
    lengths = defaultdict(deque)
    for event in SCHEMA.Event.read_multiple_bytes(bz2.decompress((base / 'raw_log.bz2').read_bytes())):
        if event.which() != 'can':
            continue
        for frame in event.can:
            data = bytes(frame.dat)
            if len(data) > 8:
                raise ValueError('Unexpected CAN payload longer than eight bytes')
            lengths[(frame.src, frame.address, data.ljust(8, b'\0'))].append(len(data))
    if sum(map(len, lengths.values())) != n:
        raise ValueError(f'Raw-log/array record count mismatch: {segment}')
    long_path = output / 'long/by_source_ms/CAN' / (source + '.ndjson')
    part = output / '.parts' / source
    part.mkdir(parents=True)
    handles, wide_counts, counts, skipped = {}, Counter(), Counter(raw_records=n), Counter()
    lookups = {ch: {msg.frame_id: msg for msg in db.messages} for ch, db in DBS.items()}
    types = {(ch, msg.frame_id): [value_type(s) for s in msg.signals] for ch, db in DBS.items() for msg in db.messages}
    encode = lambda obj: json.dumps(obj, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n'
    try:
        with (nullcontext(None) if wide_only else long_path.open('w', buffering=1024 * 1024)) as long_file:
            for addr, src, timestamp, padded in zip(arrays['address'], arrays['src'], arrays['t'], payloads):
                addr, src, padded = int(addr), int(src), bytes(padded)
                key = (src, addr, padded)
                queue = lengths.get(key)
                if not queue:
                    raise ValueError(f'Unmatched CAN occurrence: {segment}: {src}/{addr}/{padded.hex()}')
                dlc = queue.popleft()
                if src >= 128:
                    counts['send_receipts'] += 1
                    continue
                counts['received_frames'] += 1
                msg = lookups.get(src, {}).get(addr)
                reason = 'unknown_channel' if src not in lookups else 'unknown_id' if msg is None else 'dlc_mismatch' if dlc != msg.length else None
                if reason:
                    skipped[f'{reason}:ch{src}:id{addr}:dlc{dlc}'] += 1
                    continue
                if not math.isfinite(float(timestamp)) or timestamp < 0:
                    raise ValueError('Invalid device timestamp')
                ts = int(float(timestamp) * 1000)
                data = padded[:dlc]
                decoded = msg.decode(data, decode_choices=False)
                row = {'ts': ts, 'channel_id': src, 'source_file': source}
                for i, (signal, typ) in enumerate(zip(msg.signals, types[src, addr])):
                    value = decoded[signal.name]
                    independent = bit_value(data, signal)
                    if value != independent:
                        raise ValueError(f'Decode mismatch {segment}/{addr}/{signal.name}: {value} != {independent}')
                    value = float(value) if typ == 10 else int(value)
                    if long_file is not None:
                        record = {'ts': ts, 'msg_id': addr * 1000, 'channel_id': src}
                        if not compact:
                            record['signal_index'] = i
                        record['signal_name'] = signal.name
                        if not compact:
                            record['value_type'] = typ
                        record['value'] = value
                        long_file.write(encode(record))
                        counts['long_rows'] += 1
                    counts['decoded_signals'] += 1
                    row[signal.name] = value
                filename = f'can_{addr * 1000}_ch{src}.ndjson'
                if filename not in handles:
                    handles[filename] = (part / filename).open('w', buffering=256 * 1024)
                handles[filename].write(encode(sparse_wide(row)))
                wide_counts[filename] += 1
                counts['decoded_frames'] += 1
    finally:
        for handle in handles.values():
            handle.close()
    if any(lengths.values()):
        raise ValueError(f'Unconsumed raw CAN frames: {segment}')
    long_meta = None if wide_only else {'path': str(long_path.relative_to(output)), **check_file(long_path, counts['long_rows'])}
    for name, rows in wide_counts.items():
        check_file(part / name, rows, wide=True)
    return {'source_file': source, 'segment': segment, 'counts': dict(counts), 'skipped': dict(skipped),
            'long': long_meta, 'wide_rows': dict(wide_counts)}


def csv_write(path, rows):
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def verify(output):
    manifest = json.loads((output / 'manifest.json').read_text())
    if not manifest.get('complete'):
        raise ValueError('Conversion is incomplete')
    for item in manifest['files']:
        path = output / item['path']
        if 'rows' in item:
            actual = check_file(path, item['rows'], wide=item['path'].startswith('wide/CAN/'))
        else:
            actual = {'bytes': path.stat().st_size, 'sha256': digest(path)}
        if any(actual[k] != item[k] for k in actual):
            raise ValueError(f'Output verification failed: {path}')
    print(f"PASS: {len(manifest['files'])} output files verified: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=('convert', 'verify'), default='convert')
    parser.add_argument('--work-dir', type=Path, default=ROOT / '.work/metrics/can/source')
    parser.add_argument('--output', type=Path, default=ROOT / '.work/metrics/can/tables')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--max-segments', type=int, default=0, help='0 = all downloaded segments')
    parser.add_argument('--compact-long', action='store_true')
    parser.add_argument('--wide-only', action='store_true', help='Decode and validate all signals without writing the long representation')
    args = parser.parse_args()
    if args.command == 'verify':
        verify(args.output)
        return
    if args.workers < 1 or args.max_segments < 0:
        parser.error('workers must be positive and max-segments nonnegative')
    work, output = args.work_dir.resolve(), args.output.resolve()
    downloaded = json.loads((work / 'download-manifest.json').read_text())
    if not downloaded.get('complete'):
        raise ValueError('Download is incomplete; run the downloader first')
    for record in downloaded['resources']:
        path = work / record['path']
        if path.stat().st_size != record['bytes'] or digest(path) != record['sha256']:
            raise ValueError(f'Resource integrity mismatch: {path}')
    segments = downloaded['segments'][:args.max_segments or None]
    if not segments:
        raise ValueError('No downloaded segments')
    dbs = load_databases(work)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'complete': False, 'dataset_revision': downloaded['dataset_revision'],
                'opendbc_revision': downloaded['opendbc_revision'], 'cereal_revision': downloaded['cereal_revision'],
                'timestamp_unit': 'relative device milliseconds, truncated from source seconds',
                'wide_null_fields': 'omitted', 'compact_long': args.compact_long, 'wide_only': args.wide_only,
                'download_manifest_sha256': digest(work / 'download-manifest.json')}
    dump(output / 'manifest.json', manifest)
    if not args.wide_only:
        (output / 'long/by_source_ms/CAN').mkdir(parents=True)
    (output / 'wide/CAN').mkdir(parents=True)
    grouped = defaultdict(list)
    for record in downloaded['files']:
        relative = Path(record['path']).parts
        grouped['/'.join(relative[1:3])].append(record)
    sources, tasks = [], []
    for i, segment in enumerate(segments, 1):
        records = grouped[segment]
        if len(records) != 5:
            raise ValueError(f'Expected five source files for {segment}')
        chunk = records[0]['chunk']
        name = f'rav4_c{chunk:02d}_s{i:04d}'
        sources.append({'source_file': name, 'chunk': chunk, 'route': segment.split('/')[0], 'segment': segment.split('/')[1]})
        tasks.append((str(work), str(output), segment, name, args.compact_long, args.wide_only, records))
    started = time.monotonic()
    results, totals, skipped = [], Counter(), Counter()
    with ProcessPoolExecutor(max_workers=args.workers, initializer=init_worker, initargs=(str(work),)) as pool:
        for result in pool.map(convert_segment, tasks):
            results.append(result)
            totals.update(result['counts'])
            skipped.update(result['skipped'])
            print(f"[{len(results)}/{len(tasks)}] {result['source_file']}: {result['counts']['decoded_frames']} frames, {result['counts']['decoded_signals']} signal values", flush=True)
    print('Merging and validating wide tables...', flush=True)
    wide_names = sorted({name for result in results for name in result['wide_rows']})
    files = [result['long'] for result in results if result['long'] is not None]
    wide_index = []
    for name in wide_names:
        target = output / 'wide/CAN' / name
        rows = 0
        with target.open('wb') as dest:
            for result in results:
                if name in result['wide_rows']:
                    with (output / '.parts' / result['source_file'] / name).open('rb') as source:
                        shutil.copyfileobj(source, dest, 1024 * 1024)
                    rows += result['wide_rows'][name]
        item = {'path': str(target.relative_to(output)), **check_file(target, rows, wide=True)}
        files.append(item)
        stem = target.stem.split('_')
        channel, addr = int(stem[2][2:]), int(stem[1]) // 1000
        wide_index.append({**item, 'msg_id': addr * 1000, 'channel_id': channel, 'message': dbs[channel].get_message_by_frame_id(addr).name})
    csv_write(output / 'source_index.csv', sources)
    if not args.wide_only:
        csv_write(output / 'long/table_index.csv', [{'source_file': r['source_file'], **r['long']} for r in results])
    csv_write(output / 'wide/table_index.csv', wide_index)
    catalog = []
    for ch, db in dbs.items():
        for msg in db.messages:
            catalog.append({'channel_id': ch, 'msg_id': msg.frame_id * 1000, 'message': msg.name, 'dlc': msg.length,
                'signals': [{'name': s.name, 'signal_index': i, 'value_type': value_type(s), 'unit': s.unit,
                    'scale': s.scale, 'offset': s.offset, 'start': s.start, 'length': s.length,
                    'byte_order': s.byte_order, 'is_signed': s.is_signed,
                    'choices': {str(k): str(v) for k, v in (s.choices or {}).items()}}
                    for i, s in enumerate(msg.signals)]})
    dump(output / 'signal_catalog.json', catalog)
    dump(output / 'segment_summary.json', results)
    shutil.copytree(work / 'sources', output / 'sources')
    for path in sorted(output.rglob('*')):
        if path.is_file() and not path.is_relative_to(output / '.parts') and path.name != 'manifest.json' and path.suffix != '.ndjson':
            files.append({'path': str(path.relative_to(output)), 'bytes': path.stat().st_size, 'sha256': digest(path)})
    shutil.rmtree(output / '.parts')
    manifest.update(complete=True, segments=len(results), routes=len({r['route'] for r in sources}),
                    wide_tables=len(wide_names), counts=dict(totals), skipped=dict(skipped), files=files,
                    received_decode_coverage=totals['decoded_frames'] / totals['received_frames'],
                    every_signal_independently_checked=True, raw_occurrences_fully_matched=True,
                    output_rows_read_back=True, elapsed_seconds=time.monotonic() - started)
    dump(output / 'manifest.json', manifest)
    print(f'PASS: {dict(totals)}; {len(wide_names)} wide tables; {output}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
