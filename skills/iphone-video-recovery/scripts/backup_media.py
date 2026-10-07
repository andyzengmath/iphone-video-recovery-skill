#!/usr/bin/env python3
"""Read media candidates from a completed, unencrypted iOS backup.

SPDX-License-Identifier: MIT
"""
import argparse
import datetime
import json
import plistlib
import re
import sqlite3
import struct
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from recovery_safety import ensure_not_in_backup


def decode_metadata(blob):
    if not blob:
        return {}, None
    try:
        value = plistlib.loads(blob)
        ref = value.get('$top', {}).get('root')
        if isinstance(ref, plistlib.UID):
            value = value['$objects'][ref.data]
        if not isinstance(value, dict):
            raise ValueError('MBFile metadata is not a dictionary')
        return value, None
    except (ValueError, TypeError, KeyError, IndexError, plistlib.InvalidFileException) as exc:
        return {}, str(exc)


def timestamp(value, timezone):
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.datetime.fromtimestamp(value, timezone).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def inspect_atoms(path):
    """Seek over large mdat boxes without reading their payload."""
    boxes, position, length = [], 0, path.stat().st_size
    error = None
    with path.open('rb') as handle:
        while position < length:
            handle.seek(position)
            raw = handle.read(16)
            if len(raw) < 8:
                error = 'Incomplete top-level atom header'
                break
            size, kind = struct.unpack('>I4s', raw[:8])
            header = 8
            if size == 1:
                if len(raw) < 16:
                    error = 'Incomplete extended atom header'
                    break
                size, header = struct.unpack('>Q', raw[8:16])[0], 16
            elif size == 0:
                size = length - position
            if size < header or position + size > length:
                error = f'Invalid atom bounds at {position}: {kind!r}'
                break
            boxes.append({'type': kind.decode('ascii', errors='replace'),
                          'offset': position, 'size': size})
            position += size
    return boxes, error


def validate_backup(backup):
    if not backup.is_dir():
        raise ValueError(f'Backup folder does not exist: {backup}')
    manifest_plist = backup / 'Manifest.plist'
    if manifest_plist.is_file():
        with manifest_plist.open('rb') as handle:
            if plistlib.load(handle).get('IsEncrypted'):
                raise ValueError('Encrypted backup: use an authorized decryption workflow first')
    status = backup / 'Status.plist'
    if status.is_file():
        with status.open('rb') as handle:
            snapshot = plistlib.load(handle).get('SnapshotState')
        if snapshot is not None and snapshot != 'finished':
            raise ValueError(f'Backup is not finished: SnapshotState={snapshot!r}')
    database = backup / 'Manifest.db'
    if not database.is_file():
        raise ValueError('Manifest.db is missing; select the device backup root, not its parent')
    with database.open('rb') as handle:
        if handle.read(16) != b'SQLite format 3\x00':
            raise ValueError('Manifest.db is not plaintext SQLite; backup may be encrypted')
    if (backup / 'Manifest.db-wal').is_file():
        raise ValueError('Manifest.db has a WAL sidecar; resolve snapshot consistency before immutable reads')
    return database


def inspect_backup(backup, domains=None, timezone='UTC'):
    backup = Path(backup).resolve()
    database = validate_backup(backup)
    tz = ZoneInfo(timezone)
    connection = sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(Files)')}
        if not {'fileID', 'domain', 'relativePath', 'flags', 'file'} <= columns:
            raise ValueError('Unsupported Manifest.db Files schema')
        query = 'SELECT fileID,domain,relativePath,flags,file FROM Files WHERE flags=1'
        arguments = []
        if domains:
            query += ' AND (' + ' OR '.join("lower(domain) LIKE ? ESCAPE '!'" for _ in domains) + ')'
            for domain in domains:
                literal = domain.lower().replace('!', '!!').replace('%', '!%').replace('_', '!_')
                arguments.append('%' + literal + '%')
        records = []
        for file_id, domain, relative, flags, blob in connection.execute(query, arguments):
            if not isinstance(file_id, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', file_id):
                raise ValueError(f'Unsafe or unsupported fileID identifier: {file_id!r}')
            meta, meta_error = decode_metadata(blob)
            physical = backup / file_id[:2] / file_id
            if not physical.is_file():
                physical = backup / file_id
            exists = physical.is_file()
            if exists and not physical.resolve().is_relative_to(backup):
                raise ValueError('Backup blob resolves outside the backup root')
            size = physical.stat().st_size if exists else None
            head = b''
            if exists:
                with physical.open('rb') as handle:
                    head = handle.read(16)
            is_bmff = len(head) >= 8 and head[4:8] in {b'ftyp', b'moov', b'mdat', b'wide', b'free'}
            boxes, atom_error = inspect_atoms(physical) if is_bmff else ([], None)
            declared = meta.get('Size')
            records.append({
                'file_id': file_id, 'domain': domain, 'relative_path': relative,
                'flags': flags, 'source': str(physical) if exists else None,
                'exists': exists, 'actual_size': size, 'declared_size': declared,
                'size_mismatch': exists and declared is not None and declared != size,
                'created': timestamp(meta.get('Birth'), tz),
                'modified': timestamp(meta.get('LastModified'), tz),
                'metadata_error': meta_error, 'is_bmff': is_bmff,
                'moov_present': any(box['type'] == 'moov' for box in boxes) if is_bmff else None,
                'has_mdat': any(box['type'] == 'mdat' for box in boxes) if is_bmff else None,
                'top_level_atoms': boxes, 'bmff_error': atom_error,
            })
        return records
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=Path)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--domain', action='append', help='Literal domain substring; may be repeated')
    scope.add_argument('--all-domains', action='store_true')
    parser.add_argument('--timezone', default='UTC', help='IANA timezone for MBFile dates')
    parser.add_argument('--output', type=Path, help='New JSONL inventory, outside the backup')
    args = parser.parse_args()
    records = inspect_backup(args.backup, args.domain, args.timezone)
    handle = sys.stdout
    if args.output:
        ensure_not_in_backup(args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        handle = args.output.open('x', encoding='utf-8')
    try:
        for record in records:
            print(json.dumps(record, ensure_ascii=False), file=handle)
    finally:
        if handle is not sys.stdout:
            handle.close()
    print(json.dumps({'files': len(records),
                      'present_bytes': sum(r['actual_size'] or 0 for r in records),
                      'bmff_candidates': sum(r['is_bmff'] for r in records),
                      'missing_blobs': sum(not r['exists'] for r in records),
                      'size_mismatches': sum(r['size_mismatch'] for r in records)}), file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
