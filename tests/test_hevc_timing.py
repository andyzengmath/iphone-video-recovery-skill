import hashlib
import importlib.util
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/iphone-video-recovery/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('hevc_timing', SCRIPTS / 'hevc_timing.py')
hevc_timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hevc_timing)


def frame_hashes(path):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path),
                             '-map', '0:v:0', '-fps_mode', 'passthrough', '-f', 'framemd5', '-'],
                            check=True, capture_output=True, text=True)
    return [line.rsplit(',', 1)[-1].strip() for line in result.stdout.splitlines()
            if line and not line.startswith('#')]


def replace_tail_moov(path, moov):
    offset, original, _ = hevc_timing.load_moov(path)
    if offset + len(original) != path.stat().st_size:
        raise AssertionError('Synthetic fixture must have a tail moov')
    with path.open('r+b') as handle:
        handle.seek(offset)
        handle.write(moov.encode())
        handle.truncate()


def erase_composition_timing(path):
    _, _, moov = hevc_timing.load_moov(path)
    video = next(t for t in moov.children if t.kind == b'trak'
                 and t.find(b'hdlr').payload[8:12] == b'vide')
    table = video.find(b'stbl')
    table.children = [c for c in table.children if c.kind != b'ctts']
    video.children = [c for c in video.children if c.kind != b'edts']
    replace_tail_moov(path, moov)


def declare_mono_aac(path):
    _, _, moov = hevc_timing.load_moov(path)
    audio = next(t for t in moov.children if t.kind == b'trak'
                 and t.find(b'hdlr').payload[8:12] == b'soun')
    stsd = audio.find(b'stsd')
    entries = hevc_timing.parse_boxes(stsd.payload[8:])
    entry = next(e for e in entries if e.kind == b'mp4a')
    data = bytearray(entry.payload)
    version = struct.unpack_from('>H', data, 8)[0]
    header_size = {0: 28, 1: 44}[version]
    children = hevc_timing.parse_boxes(data[header_size:])
    # AAC-LC, 44.1 kHz, mono metadata, while the fixture packets encode stereo.
    specific = b'\x05\x02\x12\x08'
    decoder_payload = b'\x40\x15' + bytes(3) + bytes(8) + specific
    decoder = b'\x04' + bytes([len(decoder_payload)]) + decoder_payload
    payload = b'\x00\x01\x00' + decoder + b'\x06\x01\x02'
    candidates = list(children)
    waves = []
    for child in children:
        if child.kind == b'wave':
            nested = hevc_timing.parse_boxes(child.payload)
            candidates.extend(nested)
            waves.append((child, nested))
        elif child.kind == b'chan':
            child.payload = child.payload[:4] + struct.pack('>III', (100 << 16) | 1, 0, 0)
    esds = next(c for c in candidates if c.kind == b'esds')
    esds.payload = bytes(4) + b'\x03' + bytes([len(payload)]) + payload
    for wave, nested in waves:
        wave.payload = b''.join(c.encode() for c in nested)
    struct.pack_into('>H', data, 16, 1)
    entry.payload = bytes(data[:header_size]) + b''.join(c.encode() for c in children)
    stsd.payload = stsd.payload[:8] + b''.join(e.encode() for e in entries)
    replace_tail_moov(path, moov)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class TimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.original = Path(cls.fixture_dir.name) / 'synthetic.mov'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'testsrc2=size=160x90:rate=60000/1001:duration=3', '-f', 'lavfi', '-i',
                        'sine=frequency=600:sample_rate=44100:duration=3', '-c:v', 'libx265',
                        '-x265-params', 'log-level=error:pools=1:frame-threads=1:bframes=4:keyint=30:min-keyint=30',
                        '-tag:v', 'hvc1', '-video_track_timescale', '60000',
                        '-c:a', 'aac', '-ac', '2', '-shortest', str(cls.original)],
                       check=True, capture_output=True)
        cls.expected_hashes = frame_hashes(cls.original)

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.generated = self.root / 'generated_indexed.mov'
        shutil.copyfile(self.original, self.generated)
        erase_composition_timing(self.generated)

    def tearDown(self):
        self.temp.cleanup()

    def rebuild(self, **kwargs):
        return hevc_timing.rebuild_timing(self.generated, fps=Fraction(60000, 1001),
                                         work_root=self.root, **kwargs)

    def test_analysis_does_not_change_file_and_reports_b_frames(self):
        before = hashlib.sha256(self.generated.read_bytes()).hexdigest()
        report = self.rebuild()
        self.assertEqual(hashlib.sha256(self.generated.read_bytes()).hexdigest(), before)
        self.assertGreater(report['picture_types'].get('B', 0), 0)
        self.assertEqual(report['missing_picture_order_positions'], 0)
        self.assertTrue(report['safe_to_apply'])

    def test_retiming_restores_all_displayed_frames_without_changing_mdat(self):
        _, _, moov = hevc_timing.load_moov(self.generated)
        video = next(t for t in moov.children if t.kind == b'trak'
                     and t.find(b'hdlr').payload[8:12] == b'vide')
        locations = list(hevc_timing.sample_locations(video))
        before = self.generated.read_bytes()
        report = self.rebuild(apply=True)
        after = self.generated.read_bytes()
        for offset, length in locations:
            self.assertEqual(before[offset:offset+length], after[offset:offset+length])
        self.assertEqual(frame_hashes(self.generated), self.expected_hashes)
        self.assertEqual(report['video_frames'], len(self.expected_hashes))

    def test_wrong_frame_rate_blocks_apply_and_keeps_input_unchanged(self):
        before = self.generated.read_bytes()
        with self.assertRaisesRegex(ValueError, 'duration|timing'):
            hevc_timing.rebuild_timing(self.generated, fps=Fraction(30), work_root=self.root, apply=True)
        self.assertEqual(self.generated.read_bytes(), before)

    def test_cannot_apply_to_original_backup_or_outside_work_root(self):
        before = self.generated.read_bytes()
        with self.assertRaisesRegex(ValueError, 'work'):
            hevc_timing.rebuild_timing(self.generated, fps=Fraction(60000, 1001),
                                      work_root=self.root / 'other', apply=True)
        (self.root / 'Manifest.db').write_bytes(b'backup marker')
        with self.assertRaisesRegex(ValueError, 'backup'):
            self.rebuild(apply=True)
        self.assertEqual(self.generated.read_bytes(), before)

    def test_explicit_stereo_repair_corrects_metadata_and_decodes_audio(self):
        declare_mono_aac(self.generated)
        report = self.rebuild(apply=True, aac_stereo_44100=True)
        self.assertTrue(report['aac_stereo_corrected'])
        probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                                '-show_entries', 'stream=channels', '-of', 'csv=p=0', str(self.generated)],
                               capture_output=True, text=True, check=True)
        self.assertEqual(probe.stdout.strip(), '2')
        audio = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(self.generated),
                                '-map', '0:a:0', '-f', 's16le', '-'], capture_output=True)
        self.assertEqual(audio.returncode, 0, audio.stderr.decode())
        self.assertTrue(any(audio.stdout))
        self.assertEqual(frame_hashes(self.generated), self.expected_hashes)

    def test_stereo_analysis_proposes_change_without_claiming_it_was_applied(self):
        declare_mono_aac(self.generated)
        before = self.generated.read_bytes()
        report = self.rebuild(aac_stereo_44100=True)
        self.assertEqual(self.generated.read_bytes(), before)
        self.assertFalse(report['applied'])
        self.assertTrue(report['aac_stereo_requested'])
        self.assertFalse(report['aac_stereo_corrected'])

    def test_unknown_aac_config_is_preserved_by_default_and_explicit_fix_rejects_it(self):
        before = self.generated.read_bytes()
        with self.assertRaisesRegex(ValueError, 'AAC configuration'):
            self.rebuild(apply=True, aac_stereo_44100=True)
        self.assertEqual(self.generated.read_bytes(), before)
        self.assertFalse(self.rebuild()['aac_stereo_corrected'])


if __name__ == '__main__':
    unittest.main()
