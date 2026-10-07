import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/iphone-video-recovery/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('verified_export', SCRIPTS / 'verified_export.py')
verified_export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verified_export)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.fixture_dir.name) / 'synthetic.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                        'testsrc2=size=160x90:rate=10:duration=2', '-f', 'lavfi', '-i',
                        'sine=frequency=600:sample_rate=44100:duration=2', '-c:v', 'libx264',
                        '-threads', '1', '-c:a', 'aac', '-ac', '2', '-shortest', str(cls.fixture)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source with spaces.mp4'
        shutil.copyfile(self.fixture, self.source)
        self.dest = self.root / 'exports/movie.mp4'
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def export(self, **kwargs):
        return verified_export.export_file(self.source, self.dest, reserve_bytes=0, **kwargs)

    def test_actual_independent_file_matches_source_and_decodes(self):
        report = self.export()
        self.assertTrue(self.dest.is_file())
        self.assertFalse(self.dest.is_symlink())
        self.assertEqual(hashlib.sha256(self.dest.read_bytes()).hexdigest(), self.source_hash)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)
        self.assertEqual(report['source_sha256'], report['destination_sha256'])
        self.assertFalse(report['reencoded'])
        self.assertTrue(any(s['codec_type'] == 'audio' for s in report['profile']['streams']))
        self.assertEqual(len(report['playback_checks']), 3)
        self.assertTrue(all(c['exit_code'] == 0 and c['video_frames'] > 0
                            for c in report['playback_checks']))
        self.assertFalse(Path(str(self.dest) + '.partial').exists())

    def test_different_existing_destination_is_not_overwritten(self):
        self.dest.parent.mkdir()
        self.dest.write_bytes(b'keep me')
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(self.dest.read_bytes(), b'keep me')

    def test_identical_existing_file_is_verified_without_recopying(self):
        self.dest.parent.mkdir()
        shutil.copyfile(self.source, self.dest)
        before = self.dest.stat().st_mtime_ns
        report = self.export()
        self.assertTrue(report['already_present'])
        self.assertEqual(before, self.dest.stat().st_mtime_ns)

    def test_symlink_or_partial_destination_is_not_replaced(self):
        self.dest.parent.mkdir()
        self.dest.symlink_to(self.source)
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertTrue(self.dest.is_symlink())
        self.dest.unlink()
        partial = Path(str(self.dest) + '.partial')
        partial.write_bytes(b'interrupted copy')
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(partial.read_bytes(), b'interrupted copy')

    def test_output_cannot_be_written_inside_original_backup(self):
        (self.root / 'Manifest.db').write_bytes(b'fixture marker')
        with self.assertRaisesRegex(ValueError, 'backup'):
            self.export()
        self.assertFalse(self.dest.exists())

    def test_unmounted_volume_is_not_created(self):
        absent = self.root / 'not mounted'
        with self.assertRaisesRegex(ValueError, 'mounted'):
            self.export(volume=absent)
        self.assertFalse(absent.exists())
        self.assertFalse(self.dest.exists())

    def test_insufficient_space_stops_before_copy(self):
        with mock.patch.object(verified_export.shutil, 'disk_usage', return_value=(100, 99, 1)):
            with self.assertRaisesRegex(OSError, 'space'):
                self.export()
        self.assertFalse(self.dest.exists())

    def test_decode_failure_preserves_partial_and_does_not_publish_final(self):
        with mock.patch.object(verified_export, 'check_playback', side_effect=RuntimeError('bad decode')):
            with self.assertRaisesRegex(RuntimeError, 'bad decode'):
                self.export()
        self.assertFalse(self.dest.exists())
        self.assertEqual(hashlib.sha256(Path(str(self.dest) + '.partial').read_bytes()).hexdigest(),
                         self.source_hash)

    def test_report_records_validated_copy(self):
        path = self.root / 'verification.json'
        report = self.export(report_path=path)
        self.assertEqual(json.loads(path.read_text())['destination_sha256'], self.source_hash)
        self.assertEqual(report['size'], self.source.stat().st_size)


if __name__ == '__main__':
    unittest.main()
