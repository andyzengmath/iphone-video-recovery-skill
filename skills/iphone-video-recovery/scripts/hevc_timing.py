#!/usr/bin/env python3
"""Rebuild composition timing for a bounded HEVC recovery profile.

SPDX-License-Identifier: MIT
"""
import argparse
import collections
import json
import mmap
import os
from pathlib import Path
import struct
import sys
from fractions import Fraction

from recovery_safety import ensure_not_in_backup, write_json_exclusive

CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts'}


class Box:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload
        self.children = parse_boxes(payload) if kind in CONTAINERS else None

    def encode(self):
        payload = b''.join(c.encode() for c in self.children) if self.children is not None else self.payload
        return struct.pack('>I4s', 8 + len(payload), self.kind) + payload

    def find(self, kind):
        if self.kind == kind:
            return self
        for child in self.children or []:
            result = child.find(kind)
            if result is not None:
                return result
        return None


def parse_boxes(data):
    result, pos = [], 0
    while pos < len(data):
        if len(data) - pos < 8:
            raise ValueError('Incomplete atom header')
        size, kind = struct.unpack_from('>I4s', data, pos)
        header = 8
        if size == 1:
            size = struct.unpack_from('>Q', data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - pos
        if size < header or pos + size > len(data):
            raise ValueError(f'Invalid atom {kind!r}: {size}')
        result.append(Box(kind, data[pos + header:pos + size]))
        pos += size
    return result


def load_moov(path):
    with path.open('rb') as src:
        pos, length = 0, path.stat().st_size
        while pos < length:
            src.seek(pos)
            size, kind = struct.unpack('>I4s', src.read(8))
            header = 8
            if size == 1:
                size = struct.unpack('>Q', src.read(8))[0]
                header = 16
            elif size == 0:
                size = length - pos
            if size < header or pos + size > length:
                raise ValueError('Invalid root atom')
            if kind == b'moov':
                if size > 100_000_000:
                    raise ValueError('Unexpectedly large moov')
                src.seek(pos)
                raw = src.read(size)
                return pos, raw, parse_boxes(raw)[0]
            pos += size
    raise ValueError('No moov')


def media_time(box):
    scale_off, duration_off, duration_format = (20, 24, '>Q') if box.payload[0] else (12, 16, '>I')
    return struct.unpack_from('>I', box.payload, scale_off)[0], struct.unpack_from(duration_format, box.payload, duration_off)[0]


def set_media_time(box, scale, duration):
    data = bytearray(box.payload)
    scale_off, duration_off, duration_format = (20, 24, '>Q') if data[0] else (12, 16, '>I')
    struct.pack_into('>I', data, scale_off, scale)
    struct.pack_into(duration_format, data, duration_off, duration)
    box.payload = bytes(data)


def set_track_duration(box, duration):
    data = bytearray(box.payload)
    struct.pack_into('>Q' if data[0] else '>I', data, 28 if data[0] else 20, duration)
    box.payload = bytes(data)


def sample_locations(track):
    data = track.find(b'stsz').payload
    constant, count = struct.unpack_from('>II', data, 4)
    sizes = [constant] * count if constant else struct.unpack_from(f'>{count}I', data, 12)
    chunk_box = track.find(b'co64') or track.find(b'stco')
    chunks = struct.unpack_from('>I', chunk_box.payload, 4)[0]
    offsets = struct.unpack_from(f'>{chunks}{"Q" if chunk_box.kind == b"co64" else "I"}', chunk_box.payload, 8)
    table = track.find(b'stsc').payload
    entries = struct.unpack_from('>I', table, 4)[0]
    rules = [struct.unpack_from('>III', table, 8 + i * 12) for i in range(entries)]
    rule, sample = 0, 0
    for chunk, offset in enumerate(offsets, 1):
        while rule + 1 < len(rules) and rules[rule + 1][0] <= chunk:
            rule += 1
        for _ in range(rules[rule][1]):
            if sample >= count:
                raise ValueError('Chunk table has too many samples')
            size = sizes[sample]
            yield offset, size
            offset += size
            sample += 1
    if sample != count:
        raise ValueError(f'Chunk table has {sample} samples, expected {count}')


def rbsp(nal):
    return nal[2:].replace(b'\x00\x00\x03', b'\x00\x00')


class Bits:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, count):
        value = 0
        for _ in range(count):
            if self.pos >= len(self.data) * 8:
                raise ValueError('Incomplete slice header')
            value = (value << 1) | ((self.data[self.pos // 8] >> (7 - self.pos % 8)) & 1)
            self.pos += 1
        return value

    def ue(self):
        zeros = 0
        while not self.read(1):
            zeros += 1
            if zeros > 32:
                raise ValueError('Invalid Exp-Golomb code')
        return (1 << zeros) - 1 + self.read(zeros)


def hevc_parameters(track):
    stsd = track.find(b'stsd').payload
    index = stsd.find(b'hvcC')
    if index < 4:
        raise ValueError('No hvcC')
    size = struct.unpack_from('>I', stsd, index - 4)[0]
    data = stsd[index + 4:index - 4 + size]
    length_size, arrays = (data[21] & 3) + 1, data[22]
    cursor, nals = 23, {}
    for _ in range(arrays):
        kind, count = data[cursor] & 63, struct.unpack_from('>H', data, cursor + 1)[0]
        cursor += 3
        for _ in range(count):
            length = struct.unpack_from('>H', data, cursor)[0]
            cursor += 2
            if cursor + length > len(data):
                raise ValueError('Truncated hvcC parameter set')
            if kind in nals and kind in (32, 33, 34):
                raise ValueError('Multiple HEVC parameter sets are unsupported')
            nals[kind] = data[cursor:cursor + length]
            cursor += length
    s = Bits(rbsp(nals[33]))
    s.read(4)
    sublayers = s.read(3)
    s.read(1)
    s.read(96)
    flags = [(s.read(1), s.read(1)) for _ in range(sublayers)]
    if sublayers:
        s.read(2 * (8 - sublayers))
    for profile, level in flags:
        if profile:
            s.read(88)
        if level:
            s.read(8)
    sps_id, chroma = s.ue(), s.ue()
    separate_colour_plane = s.read(1) if chroma == 3 else 0
    width, height = s.ue(), s.ue()
    crop = [s.ue() for _ in range(4)] if s.read(1) else [0, 0, 0, 0]
    crop_x = 2 if chroma in (1, 2) else 1
    crop_y = 2 if chroma == 1 else 1
    width -= (crop[0] + crop[1]) * crop_x
    height -= (crop[2] + crop[3]) * crop_y
    luma_depth, chroma_depth = s.ue() + 8, s.ue() + 8
    if chroma != 1 or luma_depth != 8 or chroma_depth != 8:
        raise ValueError('Only 8-bit 4:2:0 HEVC is supported')
    poc_bits = s.ue() + 4
    if not 4 <= poc_bits <= 16:
        raise ValueError('Unsupported POC width')
    p = Bits(rbsp(nals[34]))
    pps_id, pps_sps_id = p.ue(), p.ue()
    dependent, output, extra = p.read(1), p.read(1), p.read(3)
    if separate_colour_plane or sps_id != pps_sps_id:
        raise ValueError('Unsupported HEVC parameter set')
    return dict(length_size=length_size, poc_bits=poc_bits, pps_id=pps_id,
                dependent=dependent, output=output, extra=extra, width=width, height=height)


def picture_info(fd, offset, size, parameters):
    def read(count, pos):
        return fd[pos:pos + count] if isinstance(fd, mmap.mmap) else os.pread(fd, count, pos)

    data = read(min(size, 256), offset)
    cursor, prefix = 0, parameters['length_size']
    while cursor + prefix + 3 <= size:
        if cursor + prefix + 16 > len(data):
            header = read(64, offset + cursor)
        else:
            header = data[cursor:cursor + prefix + 64]
        length = int.from_bytes(header[:prefix], 'big')
        nal = header[prefix:]
        kind = (nal[0] >> 1) & 63
        if nal[0] & 128 or ((nal[0] & 1) << 5 | nal[1] >> 3) or not (nal[1] & 7):
            raise ValueError('Unsupported or invalid HEVC NAL header')
        if kind in (16, 17, 18):
            raise ValueError('BLA access points are unsupported')
        if length <= 0 or cursor + prefix + length > size:
            raise ValueError('Invalid NAL length')
        if kind <= 9 or 16 <= kind <= 21:
            bits = Bits(rbsp(nal))
            if not bits.read(1):
                raise ValueError('First sample slice is not a new picture')
            if 16 <= kind <= 23:
                bits.read(1)
            if bits.ue() != parameters['pps_id']:
                raise ValueError('Unexpected PPS')
            bits.read(parameters['extra'])
            slice_type = bits.ue()
            if parameters['output']:
                bits.read(1)
            if kind in (19, 20):
                poc = 0
            else:
                poc = bits.read(parameters['poc_bits'])
            return kind, poc, slice_type, (nal[1] & 7) - 1
        cursor += prefix + length
    raise ValueError('No video slice in sample')


def fix_aac_channels(track):
    stsd = track.find(b'stsd')
    entries = parse_boxes(stsd.payload[8:])
    fixed = 0
    for entry in entries:
        if entry.kind != b'mp4a':
            continue
        data = bytearray(entry.payload)
        version = struct.unpack_from('>H', data, 8)[0]
        header_size = {0: 28, 1: 44}.get(version)
        if header_size is None:
            raise ValueError('Unsupported mp4a version')
        children = parse_boxes(bytes(data[header_size:]))
        candidates = list(children)
        wave_children = []
        for child in children:
            if child.kind == b'wave':
                nested = parse_boxes(child.payload)
                candidates.extend(nested)
                wave_children.append((child, nested))
            elif child.kind == b'chan':
                # Core Audio's standard stereo layout (101 << 16 | 2).
                child.payload = child.payload[:4] + struct.pack('>III', (101 << 16) | 2, 0, 0)
        for child in candidates:
            if child.kind != b'esds':
                continue
            desc = bytearray(child.payload)

            def walk(start, end):
                nonlocal fixed
                while start < end:
                    tag = desc[start]
                    start += 1
                    length = 0
                    for _ in range(4):
                        b = desc[start]
                        start += 1
                        length = (length << 7) | (b & 127)
                        if not (b & 128):
                            break
                    stop = start + length
                    if stop > end:
                        raise ValueError('Invalid MPEG-4 descriptor')
                    if tag == 3:
                        flags = desc[start + 2]
                        nested = start + 3
                        if flags & 128:
                            nested += 2
                        if flags & 64:
                            nested += 1 + desc[nested]
                        if flags & 32:
                            nested += 2
                        walk(nested, stop)
                    elif tag == 4:
                        walk(start + 13, stop)
                    elif tag == 5:
                        asc = bytes(desc[start:stop])
                        if asc not in (b'\x12\x08', b'\x12\x10'):
                            raise ValueError(f'Unexpected AAC configuration {asc.hex()}')
                        desc[start:stop] = b'\x12\x10'
                        fixed += 1
                    start = stop

            walk(4, len(desc))
            child.payload = bytes(desc)
        for wave, nested in wave_children:
            wave.payload = b''.join(c.encode() for c in nested)
        struct.pack_into('>H', data, 16, 2)
        entry.payload = bytes(data[:header_size]) + b''.join(c.encode() for c in children)
    if fixed != 1:
        raise ValueError(f'Expected one AAC configuration, got {fixed}')
    stsd.payload = stsd.payload[:8] + b''.join(e.encode() for e in entries)



def rebuild_timing(path, *, fps, work_root=None, apply=False,
                   aac_stereo_44100=False, report_path=None):
    """Analyze by default; apply only to an independent generated working file."""
    path = Path(path)
    if apply and path.is_symlink():
        raise ValueError('A repair target must not be a symlink')
    path = path.resolve()
    fps = Fraction(fps)
    if not 0 < fps <= 240:
        raise ValueError('Invalid frame rate')
    if apply:
        ensure_not_in_backup(path)
        if work_root is None or not path.is_relative_to(Path(work_root).resolve()):
            raise ValueError('Repair target must be inside the supplied work root')
        if path.stat().st_nlink != 1:
            raise ValueError('A repair target must be an independent file, not a hardlink')
    if report_path:
        ensure_not_in_backup(report_path)
        if Path(report_path).exists() or Path(report_path).is_symlink():
            raise FileExistsError('Report path is occupied')
    before = path.stat()
    moov_offset, original, moov = load_moov(path)
    tracks = [t for t in moov.children if t.kind == b'trak']
    videos = [t for t in tracks if t.find(b'hdlr').payload[8:12] == b'vide']
    audios = [t for t in tracks if t.find(b'hdlr').payload[8:12] == b'soun']
    if len(videos) != 1 or len(audios) > 1 or len(tracks) != len(videos) + len(audios):
        raise ValueError('Only one HEVC video and at most one audio track are supported')
    video, audio = videos[0], audios[0] if audios else None
    parameters = hevc_parameters(video)
    count = struct.unpack_from('>I', video.find(b'stsz').payload, 8)[0]
    if not 0 < count <= 2_000_000:
        raise ValueError('Video sample count is outside the bounded inspection limit')
    display_indices, display_pocs = [None] * count, [None] * count
    group, groups, gaps, base, pictures = [], 0, [], 0, collections.Counter()
    nal_types, poc_base = collections.Counter(), 0

    def finish_group():
        nonlocal groups, base, poc_base
        if not group:
            return
        order = sorted(group, key=lambda value: value[1])
        pocs = [poc for _, poc in order]
        if len(set(pocs)) != len(pocs):
            raise ValueError('Duplicate picture order count')
        if pocs != list(range(len(pocs))):
            gaps.append(dict(group=groups, first_sample=group[0][0], sample_count=len(group),
                             first_poc=pocs[0], last_poc=pocs[-1]))
        for rank, (sample, poc) in enumerate(order):
            display_indices[sample] = base + rank
            display_pocs[sample] = poc_base + poc
        base += len(group)
        poc_base += pocs[-1] + 1
        groups += 1
        group.clear()

    fd = os.open(path, os.O_RDONLY)
    mapped = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
    try:
        previous_lsb, previous_msb = 0, 0
        max_poc = 1 << parameters['poc_bits']
        for index, (offset, size) in enumerate(sample_locations(video)):
            if offset < 0 or size <= 0 or offset + size > len(mapped):
                raise ValueError('Video sample points outside the file')
            kind, lsb, slice_type, temporal = picture_info(mapped, offset, size, parameters)
            if slice_type not in (0, 1, 2):
                raise ValueError('Invalid slice type')
            if kind in (19, 20):
                finish_group()
                previous_lsb, previous_msb, poc = 0, 0, 0
            else:
                if not group:
                    raise ValueError('Supported profile must start with an IDR picture')
                msb = previous_msb
                if lsb < previous_lsb and previous_lsb - lsb >= max_poc // 2:
                    msb += max_poc
                elif lsb > previous_lsb and lsb - previous_lsb > max_poc // 2:
                    msb -= max_poc
                poc = msb + lsb
                if temporal == 0 and kind not in (6, 7, 8, 9):
                    previous_lsb, previous_msb = lsb, msb
            group.append((index, poc))
            pictures[['B', 'P', 'I'][slice_type]] += 1
            nal_types[kind] += 1
            if index and index % 50000 == 0:
                print(f'Parsed display order: {index}/{count} frames', file=sys.stderr, flush=True)
        finish_group()
    finally:
        mapped.close()
        os.close(fd)
    if sorted(display_indices) != list(range(count)):
        raise ValueError('Display order is not a complete permutation')

    scale, step = fps.numerator, fps.denominator
    duration = count * step
    offsets = [(display - decode) * step for decode, display in enumerate(display_indices)]
    runs = []
    for value in offsets:
        if runs and runs[-1][1] == value:
            runs[-1] = (runs[-1][0] + 1, value)
        else:
            runs.append((1, value))
    stbl = video.find(b'stbl')
    video.find(b'stts').payload = bytes(4) + struct.pack('>III', 1, count, step)
    stbl.children = [child for child in stbl.children if child.kind not in (b'ctts', b'sdtp')]
    stbl.children.append(Box(b'ctts', b'\x01\x00\x00\x00' + struct.pack('>I', len(runs)) +
                            b''.join(struct.pack('>Ii', number, value) for number, value in runs)))
    set_media_time(video.find(b'mdhd'), scale, duration)
    video.children = [child for child in video.children if child.kind != b'edts']
    movie_scale, _ = media_time(moov.find(b'mvhd'))
    video_movie_duration = round(duration * movie_scale / scale)
    set_track_duration(video.find(b'tkhd'), video_movie_duration)
    audio_seconds = None
    if audio:
        audio_scale, audio_duration = media_time(audio.find(b'mdhd'))
        audio_seconds = audio_duration / audio_scale
    if aac_stereo_44100:
        if audio is None:
            raise ValueError('No audio track exists to correct')
        fix_aac_channels(audio)
    movie_duration = max(video_movie_duration, round((audio_seconds or 0) * movie_scale))
    set_media_time(moov.find(b'mvhd'), movie_scale, movie_duration)
    rewritten = moov.encode()
    mismatch = abs(duration / scale - audio_seconds) if audio_seconds is not None else None
    safe = not gaps and (mismatch is None or mismatch <= 1.0)
    report = dict(
        video_frames=count, video_duration_seconds=duration / scale,
        audio_duration_seconds=audio_seconds, frame_rate=str(fps),
        aac_stereo_requested=bool(aac_stereo_44100), aac_stereo_corrected=False,
        hevc_parameters=parameters, picture_types=dict(pictures), nal_types=dict(nal_types),
        gop_count=groups, gops_with_missing_poc=len(gaps), missing_poc_examples=gaps[:15],
        composition_offset_range=[min(offsets), max(offsets)], composition_runs=len(runs),
        first_display_indices=display_indices[:30], picture_order_span=max(display_pocs) + 1,
        missing_picture_order_positions=max(display_pocs) + 1 - count,
        duration_difference_seconds=mismatch, safe_to_apply=safe,
        original_moov_offset=moov_offset, original_moov_size=len(original),
        new_moov_size=len(rewritten), applied=False,
    )
    if apply:
        if not safe:
            raise ValueError('Unsafe timing: POC gaps or video/audio duration mismatch; input was not changed')
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError('Working file changed during analysis')
        saved_moov = Path(str(path) + '.moov-before-timing.bin')
        ensure_not_in_backup(saved_moov)
        with saved_moov.open('xb') as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        with path.open('r+b') as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(rewritten)
            handle.flush()
            os.fsync(handle.fileno())
            # The old moov retains its byte length, preserving all chunk offsets.
            handle.seek(moov_offset + 4)
            handle.write(b'free')
            handle.flush()
            os.fsync(handle.fileno())
        report['applied'] = True
        report['aac_stereo_corrected'] = bool(aac_stereo_44100)
    if report_path:
        write_json_exclusive(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('generated_file', type=Path)
    parser.add_argument('--fps', required=True, type=Fraction,
                        help='Evidence-supported constant rate, e.g. 60000/1001')
    parser.add_argument('--work-root', type=Path, help='Required when applying changes')
    parser.add_argument('--apply', action='store_true', help='Modify an independent generated working file')
    parser.add_argument('--aac-stereo-44100', action='store_true',
                        help='Only for independently confirmed AAC-LC 44.1 kHz stereo packets')
    parser.add_argument('--report', type=Path, help='New JSON analysis or repair report')
    args = parser.parse_args()
    result = rebuild_timing(args.generated_file, fps=args.fps, work_root=args.work_root,
                            apply=args.apply, aac_stereo_44100=args.aac_stereo_44100,
                            report_path=args.report)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, IndexError, struct.error) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
