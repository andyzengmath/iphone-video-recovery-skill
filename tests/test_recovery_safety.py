import errno
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/iphone-video-recovery/scripts'
spec = importlib.util.spec_from_file_location('recovery_safety', SCRIPTS / 'recovery_safety.py')
safety = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safety)


class PublicationTests(unittest.TestCase):
    def test_publish_never_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            partial, final = Path(directory) / 'partial', Path(directory) / 'final'
            partial.write_bytes(b'new verified copy')
            final.write_bytes(b'old file')
            with self.assertRaises(FileExistsError):
                safety.publish_no_replace(partial, final)
            self.assertEqual(final.read_bytes(), b'old file')
            self.assertEqual(partial.read_bytes(), b'new verified copy')

    @unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'),
                         'Native exclusive rename test')
    def test_no_hardlink_filesystem_fallback_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            partial, final = Path(directory) / 'partial', Path(directory) / 'final'
            partial.write_bytes(b'new verified copy')
            with mock.patch.object(safety.os, 'link', side_effect=OSError(errno.EPERM, 'no hardlinks')):
                safety.publish_no_replace(partial, final)
            self.assertEqual(final.read_bytes(), b'new verified copy')
            self.assertFalse(partial.exists())
            partial.write_bytes(b'another file')
            with mock.patch.object(safety.os, 'link', side_effect=OSError(errno.EPERM, 'no hardlinks')):
                with self.assertRaises(FileExistsError):
                    safety.publish_no_replace(partial, final)
            self.assertEqual(final.read_bytes(), b'new verified copy')
            self.assertEqual(partial.read_bytes(), b'another file')


if __name__ == '__main__':
    unittest.main()
