---
name: iphone-video-recovery
description: Use when recovering interrupted or missing iPhone recordings from a local Finder, iTunes, or i4Tools backup, especially Insta360 Flow clips with missing moov, HEVC display-order defects, or large exports to an external drive.
---

# iPhone Video Recovery

Find surviving recordings in a completed iPhone backup, distinguish personal footage from downloaded templates, and produce independently saved, verified media. A clip absent from both albums can still survive in an app's private Library. A successful recovery-tool exit alone does not establish correct motion, sound, or completeness.

## Scope and tools

The bundled inventory helper supports completed, **unencrypted backups with Manifest.db**. Encrypted or legacy backups need an appropriate authorized reader first. This is recovery from surviving backup files; it does not scan deleted iPhone flash storage or recover files absent from the backup.

Use Python 3.10+, `ffmpeg`, and `ffprobe`. Run helpers through `python3 -B` to avoid writing bytecode beside the installed skill; resolve their paths against this skill directory. Reference-based container reconstruction optionally uses upstream `anthwlock/untrunc`. Read [backup-layout.md](references/backup-layout.md) for backup/database details and [container-repair.md](references/container-repair.md) only when a candidate needs repair.

## Establish the available evidence

Use the phone/app, actual capture date and timezone, approximate recording length/settings, backup location, and chosen output disk already supplied by the user. Ask only for missing details that change recovery decisions. For a live incident, preserve the app's data and obtain a backup before clearing caches, reinstalling, or making a test recording in the affected app.

“Documents & Data” includes more than the file-sharing Documents view. Live access denied to `Library` is distinct from macOS permission to write an external disk. Do not treat either message as evidence that the recording was deleted. System `Library/Caches` and `tmp` are normally excluded from backup; inspect what the completed backup actually contains. [Apple's directory guidance](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/FileSystemProgrammingGuide/FileSystemOverview/FileSystemOverview.html) describes these differences.

## Inventory and identify

1. Find the **device backup root** containing `Manifest.db`; verify backup completion and encryption status. Open the manifest read-only. `immutable=1` is appropriate for a completed, stable snapshot without a pending WAL, not a backup still being written.
2. Inventory the relevant app domain, then expand to CameraRoll and other domains when locating originals, compatible references, or additional recordings:

   ~~~bash
   python3 -B scripts/backup_media.py "/path/to/device-backup" --domain insta360 \
     --timezone America/New_York --output "/path/to/work/app-inventory.jsonl"
   python3 -B scripts/backup_media.py "/path/to/device-backup" --domain CameraRollDomain \
     --output "/path/to/work/camera-inventory.jsonl"
   ~~~

3. Resolve manifest `fileID` values to actual hashed files. Compare declared and physical size; retain missing-file and malformed-metadata findings. Search logical paths **and media headers**, since hashed names and extensionless resources hide videos.
4. For Insta360, observed candidate paths include `Library/COMMON_DISK_CACHE/file/FMG/video/Flow_*.MOV` and `Library/private/new_movie_master/resource/*.mp4`. These are search clues, not a stable app API. Extend the search rather than declaring loss when they are absent.
5. Probe duration, codec, dimensions, rate, and audio; inspect representative frames where needed. Numerous tiny `downloadCache`/network MP4s may be templates rather than personal recordings. Compare app album records and Photos metadata to original filenames and asset links. Deduplicate cache/original versions before counting additional recordings.

Filename time, backup MBFile time, container creation time, and capture time can differ. In particular, a recently modified cache can contain old footage. State which timestamp supports a date and convert with the appropriate timezone; do not relabel it as capture time.

## Choose the operation and disk budget

| Evidence | Operation |
|---|---|
| Healthy, identified clip | Export unchanged bytes with `verified_export.py`. |
| Missing `moov`, surviving media payload | Follow the container-repair reference on generated working files. |
| Repaired HEVC has motion glitches or incorrect duration | Check VCL grouping, POC-derived composition timing, and frame counts. |
| Audio exists but sounds wrong or nearly silent | Check encoded AAC configuration separately from the video reference. |
| Source has no audio stream | Export it as video-only; container repair cannot invent its missing sound. |

Budget **peak simultaneous bytes**, including damaged source copies, tool outputs, final remux, other exports, and reserve. Schedule large operations sequentially and use the selected external drive when the Mac cannot fit them. Do not delete a source or recovery intermediate merely to make space before its successor is verified.

Verify the output volume is mounted, writable, and able to store the largest individual file. An absent `/Volumes/NAME` must not become an ordinary folder on the Mac. If a filesystem call is waiting on a macOS removable-volume prompt, have the user allow the responsible app and resume the existing job. A blocked call is not proof of a broken filesystem; avoid starting duplicate copies.

## Export and verify

~~~bash
python3 -B scripts/verified_export.py "/path/to/healthy-or-repaired.MOV" \
  "/Volumes/RecoveryDrive/Recovered/recording.MOV" \
  --volume "/Volumes/RecoveryDrive" \
  --report "/Volumes/RecoveryDrive/Recovered/recording.verification.json"
~~~

The helper copies to an exclusive `.partial`, fsyncs, checks SHA-256 equality, probes media, and decodes start/middle/end samples before publishing an independent file. It refuses symlink destinations, occupied partials, differing existing files, and writes inside an original backup. An identical existing independent file is verified without copying again. A failed partial stays available for inspection; the helper does not automatically resume it.

An unchanged export should have matching source/destination hashes. A repaired container changes bytes, so compare encoded sample integrity/counts and validate the repaired timeline instead. Follow the repair reference for frame order, audio waveform/channel configuration, and synchronization checks. Short decode samples are not a full-file decode; describe the actual validation scope.

Report results with actual destination, duration, resolution, audio presence, recovery limits, and verification evidence. Distinguish the requested recording from additional older clips. A workspace symlink can provide a convenient link to a verified external file, but a symlink pointing into the backup does not constitute an independent export.
