import hashlib
from contextlib import closing
import importlib.util
import plistlib
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/iphone-video-recovery/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('backup_media', SCRIPTS / 'backup_media.py')
backup_media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_media)


def metadata(size, birth=1700000000):
    return plistlib.dumps({'$objects': ['$null', {'Size': size, 'Birth': birth,
                          'LastModified': birth + 100}],
                          '$top': {'root': plistlib.UID(1)}}, fmt=plistlib.FMT_BINARY)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'backup with spaces'
        self.root.mkdir()
        with closing(sqlite3.connect(self.root / 'Manifest.db')) as db, db:
            db.execute('CREATE TABLE Files(fileID TEXT PRIMARY KEY, domain TEXT, '
                       'relativePath TEXT, flags INTEGER, file BLOB)')
        (self.root / 'Manifest.plist').write_bytes(plistlib.dumps({'IsEncrypted': False}))
        (self.root / 'Status.plist').write_bytes(plistlib.dumps({'SnapshotState': 'finished'}))

    def tearDown(self):
        self.temp.cleanup()

    def add(self, relative, content, flat=False, domain='AppDomain-com.insta360.oner', declared=None):
        identifier = hashlib.sha1((domain + '-' + relative).encode()).hexdigest()
        physical = self.root / identifier if flat else self.root / identifier[:2] / identifier
        physical.parent.mkdir(exist_ok=True)
        physical.write_bytes(content)
        with closing(sqlite3.connect(self.root / 'Manifest.db')) as db, db:
            db.execute('INSERT INTO Files VALUES(?,?,?,?,?)',
                       (identifier, domain, relative, 1,
                        metadata(len(content) if declared is None else declared)))
        return physical

    def test_private_library_media_and_readonly_backup(self):
        atoms = (struct.pack('>I4s', 16, b'ftyp') + b'qt  \0\0\0\0' +
                 struct.pack('>I4s', 12, b'mdat') + b'abcd')
        source = self.add('Library/COMMON_DISK_CACHE/file/FMG/video/Flow_clip.MOV', atoms)
        before = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob('*') if p.is_file()}
        rows = backup_media.inspect_backup(self.root, domains=['insta360'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(Path(rows[0]['source']), source.resolve())
        self.assertEqual(rows[0]['actual_size'], len(atoms))
        self.assertTrue(rows[0]['is_bmff'])
        self.assertFalse(rows[0]['moov_present'])
        self.assertTrue(rows[0]['has_mdat'])
        self.assertIsNone(rows[0]['metadata_error'])
        after = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_flat_layout_and_declared_size_mismatch(self):
        source = self.add('Library/private/resource/old.mp4', b'not a movie', flat=True, declared=999)
        row = backup_media.inspect_backup(self.root)[0]
        self.assertEqual(Path(row['source']), source.resolve())
        self.assertTrue(row['size_mismatch'])
        self.assertFalse(row['is_bmff'])

    def test_domain_filter_is_literal_and_case_insensitive(self):
        self.add('Documents/a.dat', b'a', domain='AppDomain-Com.Insta360.Other')
        self.add('Documents/b.dat', b'b', domain='AppDomain-unrelated')
        self.assertEqual(len(backup_media.inspect_backup(self.root, domains=['INSTA360'])), 1)
        self.assertEqual(backup_media.inspect_backup(self.root, domains=['%']), [])

    def test_encrypted_or_incomplete_backups_are_not_treated_as_plaintext(self):
        (self.root / 'Manifest.plist').write_bytes(plistlib.dumps({'IsEncrypted': True}))
        with self.assertRaisesRegex(ValueError, '[Ee]ncrypted'):
            backup_media.inspect_backup(self.root)
        (self.root / 'Manifest.plist').write_bytes(plistlib.dumps({'IsEncrypted': False}))
        (self.root / 'Status.plist').write_bytes(plistlib.dumps({'SnapshotState': 'in progress'}))
        with self.assertRaisesRegex(ValueError, 'finished|complete'):
            backup_media.inspect_backup(self.root)

    def test_missing_blob_and_malformed_metadata_are_reported(self):
        source = self.add('Library/video/clip.mov', b'1234')
        source.unlink()
        with closing(sqlite3.connect(self.root / 'Manifest.db')) as db, db:
            db.execute('UPDATE Files SET file=?', (b'broken plist',))
        row = backup_media.inspect_backup(self.root)[0]
        self.assertFalse(row['exists'])
        self.assertIsNone(row['source'])
        self.assertTrue(row['metadata_error'])

    def test_manifest_identifier_cannot_escape_backup(self):
        with closing(sqlite3.connect(self.root / 'Manifest.db')) as db, db:
            db.execute('INSERT INTO Files VALUES(?,?,?,?,?)',
                       ('../../outside', 'AppDomain-example', 'Library/a.mov', 1, metadata(1)))
        with self.assertRaisesRegex(ValueError, 'fileID|identifier'):
            backup_media.inspect_backup(self.root)


if __name__ == '__main__':
    unittest.main()
