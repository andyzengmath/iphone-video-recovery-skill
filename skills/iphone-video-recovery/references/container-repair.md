# Interrupted MOV/MP4 repair

## Missing movie index

`ffprobe` reporting a missing `moov` establishes that ordinary demuxing cannot find the index. It does not establish that media bytes are absent. Inspect top-level `ftyp`, `wide`/`free`, and `mdat` boxes, physical length, and actual payload. Normal `ffmpeg -c copy` needs enough demuxing metadata; it is not a general way to reconstruct a missing index.

Choose a healthy reference from the same phone/app mode if possible. Compare HEVC/H.264 profile, dimensions, bit depth, pixel format, NAL length format, rate, and audio codec/sample rate/channel configuration. Similar resolution alone is insufficient. **Video and audio compatibility are separate**: a video-compatible reference can declare mono AAC while the damaged recording encoded stereo.

If HEVC codec headers are absent from the damaged file, test candidate VPS/SPS/PPS sets against a small sample of its actual encoded slices. Deduplicate candidates by parameter-set signature. Convert validated length-prefixed slices to Annex B, prepend candidate parameter sets, and require actual decoded frames plus clean errors. A sample that still contains its own VPS/SPS/PPS can override the candidate headers and invalidate this compatibility experiment. A short decoder-compatible sample narrows candidates; it does not prove the whole recording or audio configuration matches.

## Reconstruct on the selected work disk

Plan peak disk use before repair. For a 13.4 GB damaged file, keeping a working copy, an indexed intermediate, and a final remux can require roughly 40 GB in addition to other exports and reserve. Tool output location matters as much as final destination. Run one large trial at a time and preserve the original backup.

The tested upstream tool is [anthwlock/untrunc](https://github.com/anthwlock/untrunc), pinned here to commit `9d86ec9ef2ffed1bf8131abe80742c0574db52b6`. It uses a healthy reference to reconstruct damaged containers. Its tool flags and FFmpeg build compatibility are version-dependent; inspect the actual help/version and upstream build guidance before running. Record the source commit, applied diff, build/library versions, and executable checksum; a `dirty` version string does not identify its modifications. The original recovery worked with FFmpeg 8.1.2, but the upstream internal-codec ABI caveat still applies to other builds.

```bash
untrunc -n -dst "/path/to/external-work/indexed.MOV" \
  "/path/to/compatible-reference.MOV" "/path/to/damaged-working-copy.MOV"
```

Use a new destination and retain logs. Select skip-unknown flags only when evidence requires them: skipping bytes may skip genuine samples. Do not use stretch-to-audio as a substitute for diagnosing a bad frame index. Start with a bounded trial containing complete packet/sample boundaries before spending time and space on the full recording.

## iPhone HEVC VCL grouping defect

One observed untrunc failure omitted or misgrouped iPhone B-frame slices. Two corrections were needed:

1. Recognize valid VCL NAL types `0..9` and `16..21`, including TSA/STSA/RADL pictures.
2. Compare layer/temporal identity between slices only **after a slice has been seen in the current access unit**. A preceding non-VCL prefix is not the previous picture's slice.

The narrow [patch](../patches/untrunc-hevc-vcl.patch) targets only the pinned commit. Check the source and symptoms first. It is not a universal “improvement” to every upstream release. Apply with `git apply --check` before `git apply`, rebuild using the upstream build instructions, and validate a small trial. The patch is GPL-2.0-or-later; its [license](../patches/COPYING) travels with this skill. Upstream code/binaries are not vendored.

## Display order and timing

Recovered HEVC can decode yet play in the wrong order if the index treats decode order as display order. Reference `ctts`, `sdtp`, or frame timing describes the reference clip, not the recovered pictures. Investigate POC, temporal IDs, sample boundaries, frame counts, and audio duration before changing the timeline.

The bundled helper implements a **bounded profile**, not all HEVC:

- one 8-bit 4:2:0 HEVC video, at most one audio track, no other tracks;
- one SPS/PPS configuration, supported first-slice headers and length-prefixed samples;
- an initial IDR picture, supported IDR/CRA sequences, at most two million video samples;
- an independently supported constant frame rate, not inferred from a bad repaired duration;
- unique, contiguous POC within IDR-delimited spans;
- video/audio durations agreeing within one second when audio exists.

It handles POC LSB wraparound, orders pictures, and builds signed version-1 `ctts` offsets. `stts` uses the supplied rational frame interval, and media/movie durations are updated. The old `moov` becomes same-length `free` while a replacement is appended, preserving sample offsets and payload bytes. Apply is refused for backup-contained targets, symlinks, hardlinks, unsupported profiles, POC gaps, and timing disagreement. It does not silently drop metadata-only or damaged samples.

Analyze first, with the rate supported by actual recording/reference evidence:

```bash
python3 -B scripts/hevc_timing.py "/path/to/external-work/indexed.MOV" \
  --fps 60000/1001 --report "/path/to/external-work/timing-analysis.json"
```

Inspect `safe_to_apply`, picture counts/types, NAL types, gaps, composition offsets, and duration difference. The `60000/1001` value matched the observed case; do not apply it to unrelated footage. If supported:

```bash
python3 -B scripts/hevc_timing.py "/path/to/external-work/indexed.MOV" \
  --fps 60000/1001 --work-root "/path/to/external-work" --apply \
  --report "/path/to/external-work/timing-applied.json"
```

Analysis defaults to read-only. Apply saves the old `moov` beside the generated file before changing that file. A failure or unsafe report calls for examining indexing/parameters, not bypassing the guards.

## AAC configuration

An audible-track declaration does not prove correct sound. In the observed case, a video-compatible reference copied mono AAC metadata onto stereo packets, producing misleadingly quiet audio. The exact AAC-LC 44.1 kHz configurations were `12 08` (mono) and `12 10` (stereo). Verify the encoded channel elements or another independent audio-configuration source, then compare decoded PCM/channel behavior; do not change configuration simply because a clip is quiet.

Only after that evidence, add `--aac-stereo-44100` to the analyze/apply calls. This narrow option accepts only those two-byte configurations and updates the AAC decoder configuration, sample-entry channel count, and supported Core Audio layout. Other rates, AAC variants, descriptor forms, or layouts need a separate diagnosis. By default, audio configuration is preserved. This option cannot recover absent audio packets or an absent audio track.

## Remux and assess the result

Rewrap into a new final container, keeping encoded packets:

```bash
ffmpeg -hide_banner -nostdin -v warning -n \
  -i "/path/to/external-work/indexed.MOV" -map 0:v:0 -map '0:a?' \
  -c copy -movflags +faststart "/path/to/external-work/final.MOV"
```

Inspect warnings, stream metadata, frame/packet counts, monotonic display timestamps, and beginning/middle/end playback through the last available frame. For suspected audio defects, decode short PCM intervals, inspect both channels' peaks/RMS and listen where practical. A quiet ending can be real; no decode error or nonzero RMS alone establishes natural sound. Inspect A/V synchronization too.

Also compare audio start time, edit lists, encoder priming, and tail padding when an intact equivalent exists. Reconstruction may lose AAC delay/edit metadata despite retaining every packet; the independent synthetic test exposed 23.22 ms of extra audio in that situation. Prefer the intact original when payload identity is established. Without original timing metadata, report the remaining timing uncertainty instead of declaring sample-exact sound merely from packet counts.

A complete stream-copy remux is evidence that packets were processed, not a complete visual/audio decode. The [FFmpeg manual](https://ffmpeg.org/ffmpeg.html) explains stream copy and mapping. Use broader decoding when observed damage requires it, and label sampled checks honestly. Once checks pass, use `verified_export.py` to place the final independent file on the requested drive.

Count agreement and continuous POC support completeness of the indexed surviving bitstream. They cannot prove that footage after the interruption, or never written to disk, was recovered. Report actual duration and any lost tail or unresolved defects.
