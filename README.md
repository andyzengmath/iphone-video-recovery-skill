# iPhone video recovery skill

A reusable Codex/agent skill for finding and recovering interrupted iPhone recordings from local backups, developed from an Insta360 Flow recovery.

The workflow found a 90-minute MOV missing its index in a private app Library, rebuilt its container and HEVC display timeline, corrected a mismatched AAC stereo configuration, and verified recovered media without re-encoding. It also exported two intact older videos with source/destination hash equality. The repository contains the reusable instructions and tools; no personal media, backup manifests, device identifiers, or recovery logs are included.

## Install

Clone this repository and copy `skills/iphone-video-recovery` into your agent's skills directory. For a fresh Codex install:

```bash
git clone https://github.com/andyzengmath/iphone-video-recovery-skill.git
mkdir -p ~/.codex/skills
cp -R iphone-video-recovery-skill/skills/iphone-video-recovery ~/.codex/skills/
```

Other agents can use the same `SKILL.md` folder in their supported skills directory.

## Use

> Use $iphone-video-recovery to find yesterday's interrupted Insta360 recording in my iPhone backup and export verified files to my external drive.

> 用 $iphone-video-recovery 检查我的 iPhone 备份，恢复 Insta360 中断的录像，并导出到指定外接硬盘。

The [skill entrypoint](skills/iphone-video-recovery/SKILL.md) routes to backup inspection and advanced repair references. Helpers support:

- Read-only modern unencrypted backup inventory, including private Library files and hashed media.
- Guarded HEVC composition-timing reconstruction for the documented bounded profile.
- Byte-preserving export with SHA-256 and sampled playback checks, without overwriting existing files.

Requirements: Python 3.10+, FFmpeg/ffprobe (6+ recommended; tested locally with 8.1.2). Optional container reconstruction uses upstream untrunc and the narrowly scoped, pinned patch. The skill does not promise recovery of data missing from the backup, decrypt encrypted backups, or apply one video's frame rate/audio configuration to all recordings.

## Tests

With FFmpeg including `libx264`/`libx265` available:

```bash
python3 -B -m unittest discover -s tests -v
```

Tests generate synthetic media locally. They cover backup preservation, private/flat layouts, encrypted/incomplete backups, HEVC B-frame display order and payload preservation, AAC configuration, disk/mount checks, and non-overwriting export. No video fixtures are tracked.

## License

The skill documentation and Python helpers use the [MIT license](LICENSE). The untrunc patch uses GPL-2.0-or-later, matching upstream; its [COPYING](skills/iphone-video-recovery/patches/COPYING) is included. Upstream source and executable binaries are not included.
