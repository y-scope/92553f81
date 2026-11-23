#!/usr/bin/env python3
"""Download pinned comma2k19 CAN inputs without videos; verify cached files.

Standard library only. This prepares source data; it does not decode signals or
produce long/wide NDJSON tables. See docs/metrics-datasets.md.
Run directly with no arguments to download Chunks 1 and 2 into the repo's
.work/metrics/can/source directory. Use `verify` to check existing files offline.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib

DEFAULT_WORK_DIR = Path(__file__).resolve().parents[2] / '.work/metrics/can/source'

DATA_REV = '4bff77c7254c654c28d4c2726186b4e825adccee'
DBC_REV = 'ec2a7dcf2fb3550c30ab6be32577547e0247c7ca'
CEREAL_REV = 'ab79999e5dc2de25c3eb0c9acbed47c9e81f5d79'
DATA_DOC_REV = '4c7f1a6e1957745beadc1def0e7225f559b09a2a'
DATA_BASE = f'https://huggingface.co/datasets/commaai/comma2k19/resolve/{DATA_REV}'
CHUNKS = {1: 8731252405, 2: 9054474112}
MEMBERS = {'raw_log.bz2', *(f'processed_log/CAN/raw_can/{n}' for n in ('src', 'data', 't', 'address'))}
RESOURCES = [
    ('sources/comma2k19/README.md', f'{DATA_BASE}/README.md'),
    ('sources/comma2k19/LICENSE',
     f'https://raw.githubusercontent.com/commaai/comma2k19/{DATA_DOC_REV}/LICENSE'),
    ('sources/comma2k19/README.github.md',
     f'https://raw.githubusercontent.com/commaai/comma2k19/{DATA_DOC_REV}/README.md'),
    *[(f'sources/opendbc/{name}', f'https://raw.githubusercontent.com/commaai/opendbc/{DBC_REV}/{name}')
      for name in ('LICENSE', 'toyota_rav4_2017_pt_generated.dbc', 'toyota_adas.dbc')],
    *[(f'sources/cereal/{name}', f'https://raw.githubusercontent.com/commaai/cereal/{CEREAL_REV}/{name}')
      for name in ('LICENSE', 'log.capnp', 'car.capnp', 'maptile.capnp', 'include/c++.capnp', 'include/java.capnp')],
]
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_UNTIL = 0.0


def request(url, start=None, end=None, total=None):
    """Strict range checks prevent an ignored Range header from downloading videos."""
    global _COOLDOWN_UNTIL
    headers = {'User-Agent': 'public-can-downloader/1', 'Accept-Encoding': 'identity'}
    limit = 16 * 1024 * 1024
    if start is not None:
        headers['Range'] = f'bytes={start}-{end-1}'
        limit = end-start
        # Give caches a distinct URL for each range; do not persist signed redirect URLs.
        url += ('&' if '?' in url else '?') + f'can_range={start}-{end-1}'
    for attempt in range(10):
        while True:
            with _COOLDOWN_LOCK:
                remaining = _COOLDOWN_UNTIL-time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 30))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                if start is not None:
                    expected = f'bytes {start}-{end-1}/{total}'
                    if response.status != 206 or response.headers.get('Content-Range') != expected:
                        raise ValueError(f'Server did not honor exact byte range {start}:{end}')
                payload = response.read(limit+1)
            if len(payload) > limit or (start is not None and len(payload) != limit):
                raise ValueError('Unexpected response size')
            return payload
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 9:
                raise
            if error.code == 429:
                # Share the server's cooldown across all workers to avoid a retry storm.
                try:
                    delay = max(1.0, float(error.headers.get('Retry-After', '60')))+1
                except ValueError:
                    delay = 61.0
                with _COOLDOWN_LOCK:
                    _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic()+delay)
                print(f'HTTP 429: pausing downloads for {delay:.0f}s before retry.', flush=True)
                continue
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 9:
                raise
        time.sleep(min(2**attempt, 8))
    raise RuntimeError('HTTP retries exhausted')


def atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        temp.write_bytes(payload)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False)+'\n').encode())


def digest_file(path):
    sha = hashlib.sha256()
    crc = 0
    size = 0
    with path.open('rb') as source:
        while data := source.read(1024*1024):
            sha.update(data)
            crc = zlib.crc32(data, crc)
            size += len(data)
    return size, crc & 0xffffffff, sha.hexdigest()


class RemoteZip(io.RawIOBase):
    """Seekable, range-only reader for Python's ZIP/ZIP64 directory parser."""
    def __init__(self, url, size):
        self.url, self.size, self.position = url, size, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        if whence not in (0, 1, 2):
            raise ValueError('Invalid seek mode')
        position = offset + (0 if whence == 0 else self.position if whence == 1 else self.size)
        if position < 0:
            raise ValueError('Negative ZIP seek')
        self.position = position
        return position

    def read(self, size=-1):
        end = self.size if size < 0 else min(self.size, self.position+size)
        if end <= self.position:
            return b''
        if end-self.position > 32*1024*1024:
            raise ValueError('Unexpectedly large ZIP directory read')
        data = request(self.url, self.position, end, self.size)
        self.position = end
        return data


def index_chunk(root, chunk):
    url = f'{DATA_BASE}/raw_data/Chunk_{chunk}.zip'
    size = CHUNKS[chunk]
    # Always read the small remote directory; it defines the complete selected source inventory.
    with zipfile.ZipFile(RemoteZip(url, size)) as archive:
        members = [{'name': item.filename, 'bytes': item.file_size,
                    'compressed_bytes': item.compress_size, 'crc32': item.CRC,
                    'compression': item.compress_type, 'offset': item.header_offset,
                    'flags': item.flag_bits} for item in archive.infolist()]
    result = {'url': url, 'archive_bytes': size, 'members': members}
    write_json(root/f'chunk-{chunk}-index.json', result)
    return result


def safe_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')) or '\\' in name:
        raise ValueError(f'Unsafe member path: {name!r}')
    return path


def select_segments(indexes, maximum):
    segments = {}
    for chunk, index in indexes.items():
        grouped = {}
        for entry in index['members']:
            if entry['name'].endswith('/'):
                continue
            parts = safe_path(entry['name']).parts
            if len(parts) < 4 or parts[0] != f'Chunk_{chunk}':
                continue
            relative = '/'.join(parts[3:])
            if relative not in MEMBERS:
                continue
            key = '/'.join(parts[1:3])
            if relative in grouped.setdefault(key, {}):
                raise ValueError(f'Duplicate ZIP member: {entry["name"]}')
            grouped[key][relative] = {**entry, 'url': index['url'], 'archive_bytes': index['archive_bytes'],
                                     'chunk': chunk, 'relative': relative,
                                     'path': 'segments/'+key+'/'+relative}
        for key, files in grouped.items():
            if set(files) != MEMBERS:
                raise ValueError(f'Incomplete segment {key}: {set(files)}')
            if key in segments:
                for name, entry in files.items():
                    old = segments[key][name]
                    if (entry['bytes'], entry['crc32']) != (old['bytes'], old['crc32']):
                        raise ValueError(f'Conflicting duplicate segment: {key}')
            else:
                segments[key] = files
    keys = sorted(segments)
    if maximum:
        keys = keys[:maximum]
    return {key: segments[key] for key in keys}


def member_payload(entry):
    url, offset, size = entry['url'], entry['offset'], entry['archive_bytes']
    header = request(url, offset, offset+30, size)
    fields = struct.unpack('<4s5H3I2H', header)
    signature, version, flags, codec, _, _, crc, compressed, raw, name_size, extra_size = fields
    if signature != b'PK\x03\x04' or flags & 1 or codec not in (0, 8):
        raise ValueError('Unsupported or invalid ZIP local header')
    if codec != entry['compression'] or flags != entry['flags']:
        raise ValueError('ZIP local/central header disagreement')
    start = offset+30
    end = start+name_size+extra_size+entry['compressed_bytes']
    if end > size or entry['bytes'] > 256*1024*1024 or end-start > 256*1024*1024:
        raise ValueError('Unexpected CAN member bounds')
    data = request(url, start, end, size)
    name = data[:name_size].decode('utf-8' if flags & 2048 else 'cp437')
    if name != entry['name']:
        raise ValueError('ZIP member name disagreement')
    compressed_data = data[name_size+extra_size:]
    if codec == 8:
        decoder = zlib.decompressobj(-15)
        payload = decoder.decompress(compressed_data, entry['bytes']+1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('Invalid deflate member boundary')
    else:
        payload = compressed_data
    if len(payload) != entry['bytes'] or zlib.crc32(payload) & 0xffffffff != entry['crc32']:
        raise ValueError('CAN member size/CRC32 mismatch')
    return payload


def download_member(root, entry, previous):
    path = root/entry['path']
    reused = False
    if path.is_file():
        size, crc, sha = digest_file(path)
        reused = size == entry['bytes'] and crc == entry['crc32']
        old = previous.get(entry['path'])
        if old is not None:
            reused = reused and sha == old['sha256']
    if not reused:
        payload = member_payload(entry)
        atomic_write(path, payload)
        size, crc, sha = digest_file(path)
        if size != entry['bytes'] or crc != entry['crc32']:
            raise ValueError(f'On-disk member verification failed: {path}')
    return {**entry, 'sha256': sha, 'reused': reused}


def fetch_resources(root):
    records = []
    for name, url in RESOURCES:
        data = request(url)
        atomic_write(root/name, data)
        records.append({'path': name, 'url': url, 'bytes': len(data),
                        'sha256': hashlib.sha256(data).hexdigest()})
    return records


def verify(root):
    manifest = json.loads((root/'download-manifest.json').read_text())
    if not manifest.get('complete'):
        raise ValueError('Download is incomplete; rerun download first')
    for entry in manifest['files'] + manifest['resources']:
        path = root.joinpath(*safe_path(entry['path']).parts)
        size, crc, sha = digest_file(path)
        if size != entry['bytes'] or sha != entry['sha256'] or ('crc32' in entry and crc != entry['crc32']):
            raise ValueError(f'Integrity check failed: {path}')
    print(f'PASS: {len(manifest["segments"])} segments, {len(manifest["files"])} CAN files, '
          f'{sum(e["bytes"] for e in manifest["files"]):,} source bytes; CRC32/size/SHA-256 verified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('download', 'verify'), nargs='?', default='download')
    parser.add_argument('--work-dir', type=Path, default=DEFAULT_WORK_DIR,
                        help="Download directory (default: this repo's .work/metrics/can/source, "
                             "independent of the current working directory)")
    parser.add_argument('--chunks', type=int, nargs='+', choices=tuple(CHUNKS), default=[1, 2])
    parser.add_argument('--max-segments', type=int, default=0, help='0 selects all segments of the requested chunks')
    parser.add_argument('--workers', type=int, default=8, help='Concurrent member downloads; 1 is sequential')
    args = parser.parse_args()
    if args.workers < 1 or args.max_segments < 0:
        parser.error('--workers must be positive and --max-segments nonnegative')
    root = args.work_dir.resolve()
    if args.command == 'verify':
        verify(root)
        return
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root/'download-manifest.json'
    previous = {}
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        if old['dataset_revision'] != DATA_REV:
            raise ValueError('Work directory belongs to a different dataset revision')
        previous = {item['path']: item for item in old['files']}
    indexes = {chunk: index_chunk(root, chunk) for chunk in sorted(set(args.chunks))}
    segments = select_segments(indexes, args.max_segments)
    entries = [entry for files in segments.values() for entry in files.values()]
    print(f'Selected {len(segments)} segments, {len(entries)} CAN files, '
          f'{sum(e["bytes"] for e in entries):,} bytes; videos excluded.', flush=True)
    resources = fetch_resources(root)
    manifest = {'dataset_revision': DATA_REV, 'opendbc_revision': DBC_REV, 'cereal_revision': CEREAL_REV,
                'chunks': sorted(indexes), 'complete': False, 'segments': list(segments),
                'resources': resources, 'files': [], 'downloaded_at': datetime.now(timezone.utc).isoformat()}
    write_json(manifest_path, manifest)
    # Verified files survive failures and restarts. Final completeness is published only at the end.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(download_member, root, entry, previous): entry for entry in entries}
        try:
            for done, future in enumerate(as_completed(jobs), 1):
                manifest['files'].append(future.result())
                if done % 25 == 0 or done == len(entries):
                    write_json(manifest_path, manifest)
                    byte_count = sum(e['bytes'] for e in manifest['files'])
                    print(f'{done}/{len(entries)} verified files; {byte_count:,} bytes', flush=True)
        except BaseException:
            for future in jobs:
                future.cancel()
            write_json(manifest_path, manifest)
            raise
    manifest['files'].sort(key=lambda e: e['path'])
    manifest['complete'] = True
    write_json(manifest_path, manifest)
    verify(root)


if __name__ == '__main__':
    main()
