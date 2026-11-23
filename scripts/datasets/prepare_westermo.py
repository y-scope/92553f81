#!/usr/bin/env python3
"""Download pinned Westermo CSV files and convert to one NDJSON document per sample."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SOURCE = json.loads(Path(__file__).with_name('westermo-source.json').read_text())
BASE = ('https://raw.githubusercontent.com/westermo/test-system-performance-dataset/'
        + SOURCE['revision'])


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def matches_source(path, expected):
    if not path.is_file() or path.stat().st_size != expected['bytes']:
        return False
    digest = hashlib.sha1(f"blob {expected['bytes']}\0".encode())
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest() == expected['git_blob_sha1']


def download(root, expected):
    """Publish only complete files matching the pinned Git blob and size."""
    path = root / expected['path']
    if matches_source(path, expected):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.partial')
    for attempt in range(5):
        try:
            request = urllib.request.Request(BASE + '/' + expected['path'],
                                             headers={'User-Agent': 'clp-metrics-benchmark/1'})
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open('wb') as output:
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > expected['bytes']:
                        raise ValueError(f'Unexpected download size: {path}')
                    output.write(block)
            if not matches_source(temporary, expected):
                raise ValueError(f'Download checksum mismatch: {path}')
            temporary.replace(path)
            print(f'Downloaded {expected["path"]}', flush=True)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
        finally:
            temporary.unlink(missing_ok=True)


def numeric(text):
    """Keep missing measurements absent and integers exact, including large counters."""
    text = text.strip()
    if text.lower() in ('', 'nan', 'null'):
        return None
    try:
        return int(text)
    except ValueError:
        number = float(text)
        if not math.isfinite(number):
            raise ValueError(f'Non-finite measurement: {text!r}')
        return number


def convert(source, target):
    rows = 0
    with source.open(newline='') as incoming, target.open('w') as outgoing:
        reader = csv.DictReader(incoming)
        if not reader.fieldnames or reader.fieldnames[0] != 'timestamp':
            raise ValueError(f'Missing timestamp column: {source}')
        names = {name: name.replace('-', '_') for name in reader.fieldnames}
        if len(set(names.values())) != len(names) or 'system_id' in names.values():
            raise ValueError(f'Conflicting normalized CSV field names: {source}')
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'Incomplete CSV record: {source}:{rows + 2}')
            record = {'system_id': source.stem}
            for key, value in row.items():
                parsed = numeric(value)
                if parsed is not None:
                    record[names[key]] = parsed
            if type(record.get('timestamp')) is not int or record['timestamp'] < 0:
                raise ValueError(f'Invalid relative timestamp: {source}:{rows + 2}')
            outgoing.write(json.dumps(record, separators=(',', ':'), allow_nan=False) + '\n')
            rows += 1
    return {'path': target.name, 'rows': rows, 'bytes': target.stat().st_size,
            'sha256': sha256(target)}


def prepare(source_dir, output, systems=19, workers=4):
    selected = {f'data/system-{n}.csv' for n in range(1, systems + 1)} | {'README.md', 'LICENSE'}
    entries = [entry for entry in SOURCE['files'] if entry['path'] in selected]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda entry: download(source_dir, entry), entries))
    if output.exists():
        manifest = json.loads((output / 'manifest.json').read_text())
        if (not manifest.get('complete') or manifest.get('preparation_version') != 1
                or manifest['source_revision'] != SOURCE['revision']
                or manifest['systems'] != systems):
            raise ValueError('Existing Westermo preparation is incomplete or uses different inputs')
        for item in manifest['files']:
            path = output / item['path']
            if path.stat().st_size != item['bytes'] or sha256(path) != item['sha256']:
                raise ValueError(f'Prepared file has changed: {path}')
        print(f'Reusing verified Westermo NDJSON: {output}', flush=True)
        return
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'complete': False, 'source_revision': SOURCE['revision'], 'systems': systems,
                'preparation_version': 1, 'field_names': 'CSV hyphens replaced by underscores',
                'timestamp_unit': 'seconds since data collection began', 'files': []}
    manifest_path = output / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    for n in range(1, systems + 1):
        item = convert(source_dir / f'data/system-{n}.csv', output / f'system-{n}.ndjson')
        manifest['files'].append(item)
        print(f"Converted system-{n}: {item['rows']:,} records", flush=True)
    manifest.update(complete=True, source_files=[{**item, 'sha256': sha256(source_dir / item['path'])}
                                               for item in entries])
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / '.work/metrics/westermo/source')
    parser.add_argument('--output', type=Path, default=ROOT / '.work/metrics/westermo/tables')
    parser.add_argument('--systems', type=int, choices=range(1, 20), default=19)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('workers must be positive')
    prepare(args.source.resolve(), args.output.resolve(), args.systems, args.workers)


if __name__ == '__main__':
    main()
