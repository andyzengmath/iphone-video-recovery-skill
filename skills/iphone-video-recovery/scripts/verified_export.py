#!/usr/bin/env python3
"""Copy healthy or repaired media with hashes, playback checks, and no overwrite.

SPDX-License-Identifier: MIT
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from recovery_safety import ensure_not_in_backup, ensure_volume, publish_no_replace, write_json_exclusive

CHUNK_SIZE = 16 * 1024 * 1024


def progress(phase, **fields):
    print(json.dumps({'phase': phase, **fields}), file=sys.stderr, flush=True)


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def probe_media(path):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries',
        'format=duration,size:format_tags=creation_time:stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels,duration',
        '-of', 'json', str(path),
    ], capture_output=True, text=True)
    if result.returncode or result.stderr.strip():
        raise RuntimeError(f'Media probe failed: {result.stderr.strip()}')
    profile = json.loads(result.stdout)
    duration = float(profile.get('format', {}).get('duration', 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Media has no valid positive duration')
    if not any(s.get('codec_type') == 'video' for s in profile.get('streams', [])):
        raise ValueError('Media has no video stream')
    return profile


def check_playback(path, profile):
    duration = float(profile['format']['duration'])
    positions = [0.0, duration / 2, max(0.0, duration - 3)]
    checks = []
    for position in positions:
        result = subprocess.run([
            'ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-ss', str(position),
            '-i', str(path), '-t', '3', '-map', '0:v:0', '-map', '0:a?',
            '-progress', 'pipe:1', '-nostats', '-f', 'null', '-',
        ], capture_output=True, text=True)
        frames = [int(line.split('=', 1)[1]) for line in result.stdout.splitlines()
                  if line.startswith('frame=')]
        count = max(frames, default=0)
        if result.returncode or result.stderr.strip() or not count:
            raise RuntimeError(f'Playback check at {position:.3f}s failed '
                               f'(frames={count}): {result.stderr.strip()}')
        checks.append({'seconds': position, 'exit_code': result.returncode,
                       'video_frames': count, 'errors': result.stderr.strip()})
    return checks


def export_file(source, destination, *, volume=None, reserve_bytes=2 * 1024**3, report_path=None):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if not source.is_file():
        raise ValueError(f'Source file is missing: {source}')
    if reserve_bytes < 0:
        raise ValueError('reserve_bytes cannot be negative')
    ensure_not_in_backup(destination)
    ensure_volume(destination, volume)
    if destination.is_symlink():
        raise FileExistsError('Destination must be an independent file, not a symlink')
    if source == destination.resolve() or (destination.is_file() and os.path.samefile(source, destination)):
        raise ValueError('Destination is the source file or a hardlink to it')
    partial = Path(str(destination) + '.partial')
    if partial.exists() or partial.is_symlink():
        raise FileExistsError(f'Existing partial file retained; inspect before retrying: {partial}')
    if report_path:
        report_path = Path(report_path).absolute()
        ensure_not_in_backup(report_path)
        if report_path in {destination, partial, source} or report_path.exists() or report_path.is_symlink():
            raise FileExistsError('Report path is occupied or collides with a media path')
    before = source.stat()
    already_present = destination.exists()
    if already_present and (not destination.is_file() or destination.stat().st_size != before.st_size):
        raise FileExistsError('Existing destination differs in size or type; it was not changed')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not already_present and shutil.disk_usage(destination.parent)[2] < before.st_size + reserve_bytes:
        raise OSError('Insufficient destination disk space for copy plus reserve')
    source_profile = probe_media(source)
    if already_present:
        source_digest, destination_digest = checksum(source), checksum(destination)
        if source_digest != destination_digest:
            raise FileExistsError('Existing destination differs from source; it was not changed')
        candidate = destination
    else:
        digest, complete, last_update = hashlib.sha256(), 0, 0.0
        with source.open('rb') as incoming, partial.open('xb') as outgoing:
            while chunk := incoming.read(CHUNK_SIZE):
                outgoing.write(chunk)
                digest.update(chunk)
                complete += len(chunk)
                now = time.monotonic()
                if now - last_update >= 5 or complete == before.st_size:
                    progress('copy', bytes=complete, total=before.st_size)
                    last_update = now
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if partial.stat().st_size != before.st_size:
            raise RuntimeError('Copy length differs from source; partial was retained')
        source_digest = digest.hexdigest()
        progress('checksum', total=before.st_size)
        destination_digest = checksum(partial)
        if source_digest != destination_digest:
            raise RuntimeError('Copy hash differs from source; partial was retained')
        candidate = partial
    after = source.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise RuntimeError('Source changed during export; no new final file was published')
    progress('playback_checks')
    profile = probe_media(candidate)
    if profile['streams'] != source_profile['streams']:
        raise RuntimeError('Copied stream metadata differs from source')
    playback = check_playback(candidate, profile)
    if not already_present:
        shutil.copystat(source, partial)
        publish_no_replace(partial, destination)
    if destination.is_symlink() or destination.stat().st_size != before.st_size:
        raise RuntimeError('Final destination is not the expected independent file')
    report = {
        'source': str(source), 'destination': str(destination), 'size': before.st_size,
        'source_sha256': source_digest, 'destination_sha256': destination_digest,
        'profile': profile, 'playback_checks': playback, 'reencoded': False,
        'already_present': already_present,
        'validation_scope': 'byte-for-byte equality and start/middle/end decode samples',
    }
    if report_path:
        write_json_exclusive(report_path, report)
    progress('complete', destination=str(destination))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--volume', type=Path, help='Expected mounted destination volume')
    parser.add_argument('--reserve-bytes', type=int, default=2 * 1024**3)
    parser.add_argument('--report', type=Path, help='New JSON verification report')
    args = parser.parse_args()
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('ffmpeg and ffprobe must be installed and available on PATH')
    report = export_file(args.source, args.destination, volume=args.volume,
                         reserve_bytes=args.reserve_bytes, report_path=args.report)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        if isinstance(exc, PermissionError) and sys.platform == 'darwin':
            print('Check the macOS removable-volume permission prompt for the responsible app.',
                  file=sys.stderr)
        sys.exit(1)
