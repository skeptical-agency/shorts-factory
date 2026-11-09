# Shorts Factory

The 9:16 short-video pipeline as a Hermes Agent plugin: burned word-timed
captions, streaming loudness, trap-safe concat, footage QA, thumbnail
safezone and motion linting — distilled from a production pipeline that ships
daily shorts across YouTube, TikTok, Instagram, Facebook and X.

**Seven tools, all local. Zero credentials, zero cloud, zero accounts.**

| Tool | What it does |
|---|---|
| `sf_status` | ffmpeg/ffprobe + Python deps + font health check |
| `sf_loudness` | ebur128 measure · two-pass loudnorm to -14 LUFS/-2 dBTP · music ducking under VO (sidechain, `amix normalize=0`) · atempo speed changes |
| `sf_concat` | Mixed stills+video concatenation with full in-graph normalization (fps/SAR/bt709/yuv420p/audio) and post-build video-stream duration asserts — kills the silent concat killers |
| `sf_captions` | TikTok/Shorts-style word-timed burned captions: script-aligned punch groups, Pillow PNGs (no libass/drawtext needed), boundary-safe enable windows, pixel-diff verification |
| `sf_footage_audit` | Contact sheets + per-second brightness audit + full-res fine grabs + the standing review checklist for source footage |
| `sf_safezone` | Thumbnail safe zone: title text inside y=400..1500 of 1920, caption-band collision checks, simulated 16:9 grid crop |
| `sf_motion_lint` | Deterministic lint of build scripts for stutter-prone motion (zoompan, time-driven crop/overlay, per-frame PIL zoom) |

## Why a plugin and not a prompt

Every rule encoded here was paid for in production: overlays silently
vanishing after concat boundaries (`-c copy` + mixed color metadata), stills
emitting one frame and shortening the video with **no error**, mixed audio
layouts dropping 2.8s of sound, caption groups stacking on boundary frames
because `enable` is inclusive on both ends, `amix` default normalization
quietly killing voice levels, zoompan rounding slow motion to visible steps,
and hook text that plays fine in-video but is **invisible in the grid
thumbnail where the click decision happens**. A prompt remembers the advice;
these tools enforce the checks, compute exact frame counts, and fail loudly.

## Requirements

- `ffmpeg` + `ffprobe` on PATH (macOS `brew install ffmpeg` · Debian/Ubuntu
  `sudo apt install ffmpeg` · Windows `winget install Gyan.FFmpeg`).
  Any build works — captions render via Pillow PNGs, so no libass/drawtext
  needed.
- Python deps: `pillow` (declared, auto-installed). Optional: `numpy`
  (caption verification), `faster-whisper` (in-plugin transcription — or
  bring your own .srt / word-timing JSON from any STT).
- Caption font: Bebas Neue auto-discovered, else auto-downloaded once from
  google/fonts into the plugin data dir. Override via the `caption_font`
  config.

## Install

```
hermes plugins install skeptical-agency/shorts-factory --enable
```

Then ask your agent things like:

- "burn captions into clip.mp4 from my script, timings in words.srt"
- "normalize this VO to platform loudness and duck the music under it"
- "concat these cards and clips into a 1080x1920 short and check the duration"
- "audit this trailer before I cut windows from it"
- "lint my build script for stutter"

## Disclosure

- Runs entirely on your machine through local ffmpeg/ffprobe subprocesses.
- No credentials, no OAuth, no API keys, no telemetry. The only network call
  that can ever occur is the optional one-time Bebas Neue font download from
  github.com/google/fonts (skipped when a local font is found or configured).
- `sf_footage_audit` checks what is visible in a clip; it does not clear your
  rights to use it. Only process footage you own or are entitled to use.

Built by [Skeptical Agency](https://skeptical.agency). MIT licensed.
