---
name: shorts-factory
description: Build and QA 9:16 shorts with the Shorts Factory tools.
---

# Shorts Factory workflow

Executable video pipeline for Shorts/Reels/TikTok. Seven tools, all local
ffmpeg + Pillow — no credentials, no cloud, no accounts.

## When to use

- Burning word-timed captions into vertical video (`sf_captions`)
- Normalizing audio to platform loudness or ducking music under VO (`sf_loudness`)
- Concatenating mixed stills/video segments safely (`sf_concat`)
- Auditing downloaded source footage before it ships (`sf_footage_audit`)
- Checking hook/title text against the thumbnail safe zone (`sf_safezone`)
- Linting a build script for motion that will stutter (`sf_motion_lint`)

## Order of operations (production pipeline)

1. `sf_status` once per session — confirm ffmpeg/ffprobe + deps.
2. `sf_footage_audit` on any source footage you don't own, BEFORE cutting:
   coarse sheet → brightness dark-runs → fine full-res grabs of candidate
   windows. Vision-review the grabs (badges, watermarks, terminal text,
   privacy). Log exclusions.
3. Build segments. If assembling from a build script, `sf_motion_lint` the
   script FIRST — text-bearing stills get NO motion, hook cards stay static.
4. `sf_concat` segments (stills need explicit `duration`). Read the returned
   `checks` report — video-stream duration drift > 1s means a trap fired.
5. `sf_captions` with `script` ALWAYS passed (caption text comes from the
   script; the timing source never supplies wording). Timings: .srt or
   word-JSON `[{word,start,end}]`; omit `timings` only if faster-whisper is
   installed. Leave `verify=true` — the band pixel-diff catches captions
   that silently vanish after concat boundaries.
6. `sf_loudness mode=measure` on the final; if integrated is off -14±1,
   `mode=normalize target=platform`. Music under VO: `mode=duck`.
   Speed changes: `mode=tempo` (atempo, never asetrate).
7. `sf_safezone` on the hook card: title bboxes inside y=400..1500 (of
   1920), captions only in y=1500..1700. Look at `grid_crop_simulation` —
   headline fully visible, centered, legible small, picture matches the
   headline noun.

## Hard rules baked into the tools (don't work around them)

- Concat NEVER uses `-c copy` (overlays silently vanish across color-metadata
  boundaries). The tool re-encodes with fps/setsar/bt709/yuv420p normalization.
- Stills in concat need explicit duration; frame counts are computed exact
  (`round(dur*fps)`) because `-t` rounds up ~1 frame/segment and drift
  cumulates.
- Duration asserts read the VIDEO STREAM (format duration reports the audio
  length and hides one-frame-still bugs).
- Caption enable windows are capped `min(end+0.15, next_start - 1/fps)` —
  `enable` is inclusive on both ends; contiguous groups otherwise stack on
  boundary frames.
- amix is always `normalize=0` (the default quietly kills voice levels).
- Captions render as Pillow PNGs — works on ffmpeg builds without
  libass/drawtext (Homebrew).

## Pitfalls

- Whisper garbles words ("now" → "nails"); that's why `script` alignment is
  the default path. Whisper output is phonetic timing, never literal text.
- Number fragments: whisper splits "$2,499" into "$2" + ",499" — the engine
  merges `^[,.]\d` tokens before grouping.
- Vision models misread small caption text on full 1080x1920 frames; the
  verify step crops the band and pixel-diffs instead.
- A font floor that can't fit RAISES — never silently overflow. Pass shorter
  groups or a wider accent budget rather than shrinking to illegibility.
- footage_audit checks what is VISIBLE; it does not clear rights. Only use
  footage you own or are entitled to use.
