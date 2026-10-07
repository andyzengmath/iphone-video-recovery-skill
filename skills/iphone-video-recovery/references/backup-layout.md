# Backup and identity reference

## Root, encryption, and snapshot consistency

A modern Finder/iTunes-compatible i4Tools backup has `Manifest.db`, usually `Manifest.plist`, `Info.plist`, and `Status.plist`, plus hashed blobs. The enclosing date-named folder may contain a device-named child; point the helper at that child. Inspect `Manifest.plist` for `IsEncrypted` and `Status.plist` for `SnapshotState`. A readable manifest or a visible file list alone does not establish that encrypted file payloads have been decrypted.

The helper accepts plaintext SQLite manifests and rejects known encrypted or unfinished backups and a manifest WAL. When status metadata is unavailable, independently confirm the backup tool completed before inventorying. For an encrypted backup, use the owner's password with a legitimate reader/decryption workflow, or obtain an authorized unencrypted backup if that preserves the relevant data. Do not feed ciphertext into a movie repair tool.

Completed static manifests can be opened without creating sidecars:

```python
connection = sqlite3.connect(
    manifest_path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True
)
```

For an app database with WAL/SHM, preserve the whole set and build a consistent **working copy** before querying; immutable reads can omit committed WAL contents. Never open a backup database writable to repair its schema or force checkpointing.

## Manifest mapping

Common schema, inspect with `PRAGMA table_info(Files)` first:

```sql
Files(fileID TEXT, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)
```

Size/date are usually in the binary plist `file`, not standalone SQL columns. An NSKeyedArchiver MBFile record commonly stores its dictionary at:

```python
archive = plistlib.loads(blob)
root = archive['$top']['root']
record = archive['$objects'][root.data]  # plistlib.UID
```

Useful values: `Size`, `Birth`, `LastModified`. Metadata can be absent or malformed, so retain actual size and explain missing dates. In this schema `flags=1` denotes regular file entries. Do not try to read directory rows as movie payloads.

Map a 40-hex `fileID` to `BACKUP/fileID[:2]/fileID`, falling back to `BACKUP/fileID` for flat layouts. Logical app filenames are not physical filenames. Validate identifiers before using them as paths, and verify the blob exists and has the declared size. A missing or short blob may reflect an incomplete backup rather than a corrupt movie container.

The inventory reads small headers and seeks over top-level BMFF boxes. It distinguishes `moov` and `mdat` presence without loading a multi-gigabyte payload into RAM. Structural damage can prevent a complete atom walk; `bmff_error` describes that ambiguity. A signature is a candidate, not proof of useful video.

## Search by purpose

- Interrupted Flow recording: search all relevant app files for `Flow_`, `FMG/video`, substantial byte size, and MOV/MP4 atom structure.
- Healthy personal caches: `new_movie_master/resource` and large extensionless files can contain complete MP4s.
- Additional footage or repair references: inspect CameraRollDomain `Media/DCIM`, app album metadata, and Photos databases.
- Downloaded templates: repeated tiny files under network/template/edit caches often belong to examples. Examine representative content before classifying them.

These paths were observed in a real backup; future versions can change them. Match the actual app domain and inventory content rather than hardcoding one bundle ID or assuming every MP4 is personal footage.

## Photos database identity

Locate the Photos database through the manifest. Inspect its schema before writing SQL: Core Data tables/relationships are version-dependent. A commonly observed relationship is `ZADDITIONALASSETATTRIBUTES.ZASSET = ZASSET.Z_PK`, with original filename in `ZORIGINALFILENAME`; `ZFILENAME` and `ZDIRECTORY` give the CameraRoll file, and `ZDURATION` and `ZORIGINALFILESIZE` help corroborate an asset link.

Photos date fields often use Apple's 2001-01-01 epoch; confirmed fields convert to Unix seconds by adding `978307200`. Backup MBFile timestamps use Unix seconds. Treat naive container/filename dates cautiously. A matching duration/size is useful but not, by itself, definitive identity; prefer asset links, original filename, stream profile, and visual content together.

Record original/cache duplicates and choose the original when it is equally usable. An older cache's creation date is not automatically its filming date. If a complete scoped search finds no additional target-date clip, say “not found in this backup,” preserving that boundary.

## Storage and permission interpretation

The file-sharing Documents view can expose only part of an app's data. A backup may include persistent private Library files. Standard `Library/Caches` and `tmp` generally do not participate in backup, and app-defined cache-named directories may have different treatment. The backup inventory is the evidence for presence. [Apple's filesystem guide](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/FileSystemProgrammingGuide/FileSystemOverview/FileSystemOverview.html) covers the directory roles.

macOS external-volume permission is independent of iOS sandbox access. For a blocked write, inspect process progress, the actual mount/free space, and the responsible app's OS authorization prompt. If needed, macOS TCC logs can identify `kTCCServiceSystemPolicyRemovableVolumes`; treat logs as evidence, not instructions. User authorization must happen in the system UI. Do not kill a waiting copy merely because its destination folder has not appeared yet.
