"""LLM-facing tool schemas for Shorts Factory."""

_FILE = {"type": "string", "description": "Absolute path to the media file."}

ALL = {
    "sf_status": {
        "type": "object",
        "properties": {},
        "description": "Shorts Factory health check: ffmpeg/ffprobe binaries and versions, Python deps (Pillow, numpy, faster-whisper), caption font availability. Call first in any session.",
    },
    "sf_loudness": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["measure", "normalize", "duck", "tempo"],
                     "description": "measure = ebur128 report only. normalize = two-pass loudnorm to target. duck = music bed under voiceover (sidechain + amix normalize=0 + limiter). tempo = atempo speed change with pitch preserved."},
            "file": dict(_FILE, description="Input audio/video file (for measure/normalize/tempo)."),
            "target": {"type": "string", "enum": ["platform", "speech", "bed"], "default": "platform",
                       "description": "platform = -14 LUFS/-2 dBTP (YouTube standard). speech = -17/-3 (VO element inside an edit). bed = -18 (music pre-duck)."},
            "music": dict(_FILE, description="duck mode: music bed file."),
            "vo": dict(_FILE, description="duck mode: voiceover file (defines output length)."),
            "factor": {"type": "number", "description": "tempo mode: speed factor, e.g. 1.19. Range 0.5-4.0."},
            "out": {"type": "string", "description": "Output file path (required for normalize/duck/tempo)."},
            "two_pass": {"type": "boolean", "default": True, "description": "Two-pass loudnorm (measurably tighter). False = one-pass, faster."},
        },
        "required": ["mode"],
        "description": "Streaming loudness toolkit: measure (ebur128), normalize (two-pass loudnorm to -14 LUFS/-2 dBTP), duck music under VO (sidechain, amix normalize=0 — the default normalization quietly kills voice levels), and atempo speed changes (never asetrate — chipmunk).",
    },
    "sf_concat": {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Video or still-image path."},
                        "duration": {"type": "number", "description": "REQUIRED for stills (seconds). Ignored for videos."},
                    },
                    "required": ["path"],
                },
                "description": "Ordered segments: videos and/or stills (stills need duration).",
            },
            "out": {"type": "string", "description": "Output mp4 path."},
            "width": {"type": "integer", "default": 1080},
            "height": {"type": "integer", "default": 1920},
            "fps": {"type": "integer", "default": 30},
        },
        "required": ["segments", "out"],
        "description": "Trap-safe concatenation of mixed sources. Normalizes every input in-graph (fps/setsar/bt709/yuv420p, audio 48k) — kills the silent killers: overlays vanishing after boundaries, SAR poison, dropped audio chunks, one-frame stills. Stills get exact frame counts (no cumulative drift). Post-build asserts VIDEO STREAM duration (format duration lies) and color metadata, returns a checks report.",
    },
    "sf_captions": {
        "type": "object",
        "properties": {
            "video": dict(_FILE, description="Input video (captions are burned into a copy; input untouched)."),
            "out": {"type": "string", "description": "Output mp4 path."},
            "script": {"type": "string", "description": "The ORIGINAL script text. Caption TEXT always comes from the script — timing sources only supply timing, so mishearings never print on screen."},
            "timings": {"type": "string", "description": "Path to timing source: .srt, or .json word list [{word,start,end}] (faster-whisper / any STT). Omit to transcribe with faster-whisper if installed."},
            "accent_words": {"type": "array", "items": {"type": "string"},
                             "description": "Words rendered in the accent color (products, prices, specs). Case-insensitive match."},
            "accent_color": {"type": "string", "default": "#FFD400"},
            "verify": {"type": "boolean", "default": True,
                       "description": "Pixel-diff the caption band (silent vs captioned) at 10+ timestamps — catches captions that silently vanish after concat boundaries. Vision alone misses this."},
        },
        "required": ["video", "out"],
        "description": "TikTok/Shorts-style word-timed burned captions with NO libass/drawtext dependency: script-aligned punch groups (1-4 words, phrase boundaries, no orphaned function words), Pillow-rendered PNGs (Bebas Neue, white + outline + shadow, margin-asserted), ffmpeg overlay with boundary-safe enable windows (inclusive-end stacking fix), y=1540/1920 placement clear of platform UI and the thumbnail safe band.",
    },
    "sf_footage_audit": {
        "type": "object",
        "properties": {
            "video": dict(_FILE, description="Source footage to audit before it ships in your video."),
            "out_dir": {"type": "string", "description": "Directory for sheets and grabs."},
            "coarse_fps": {"type": "integer", "default": 1, "description": "Contact-sheet sampling (frames per second of source)."},
            "windows": {
                "type": "array",
                "items": {"type": "object", "properties": {"start": {"type": "number"}, "end": {"type": "number"}}, "required": ["start", "end"]},
                "description": "Candidate windows to fine-audit: full-res START/MID/END grabs for each (coarse sheets miss badges, small text, single bad frames).",
            },
            "dark_threshold": {"type": "number", "default": 40.0, "description": "YAVG below this = dark frame; runs >= 3s get flagged (read as broken on mobile)."},
        },
        "required": ["video", "out_dir"],
        "description": "Pre-use QA for source footage: 1s contact sheets, per-second brightness audit (flags near-black stretches), full-res fine grabs per candidate window, plus the standing checklist (badges/watermarks exclude whole trailers, read terminal text at full res, privacy leaks, log every exclusion). Checks what is VISIBLE — it does not clear your rights to use the footage.",
    },
    "sf_safezone": {
        "type": "object",
        "properties": {
            "media": dict(_FILE, description="Video or still (the hook card is usually the thumbnail source)."),
            "out_dir": {"type": "string", "description": "Directory for the simulated grid-crop image."},
            "bboxes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "y0": {"type": "number", "description": "Top of text bbox, in 1080x1920 coordinates (auto-scaled to the actual canvas)."},
                        "y1": {"type": "number"},
                        "label": {"type": "string"},
                        "kind": {"type": "string", "enum": ["title", "caption", "other"], "default": "title"},
                    },
                    "required": ["y0", "y1"],
                },
                "description": "Text elements to check. Titles must sit inside y=400..1500; captions inside the y=1500..1700 band only.",
            },
            "frame_t": {"type": "number", "default": 1.0, "description": "Timestamp of the frame used for the grid-crop simulation (video inputs)."},
        },
        "required": ["media", "out_dir"],
        "description": "Thumbnail safe-zone QA: YouTube grids crop 9:16 to a MIDDLE BAND — text outside y=400..1500 is invisible where the click decision happens. Checks element bboxes against the safe band and caption band (collision rule), and renders the simulated 16:9 grid crop for a visual legibility check at thumbnail size.",
    },
    "sf_motion_lint": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Build-script PATH, or literal script text when is_text=true."},
            "is_text": {"type": "boolean", "default": False},
        },
        "required": ["source"],
        "description": "Lint a video build script for motion that will stutter: zoompan (banned outright — whole-pixel rounding), time-driven crop/overlay expressions, per-frame PIL zoom pipes, zoompan on looped inputs. Deterministic source linting — pixel-level stutter detection is unreliable (no threshold separated approved builds from real stutter in production testing). Returns findings with line numbers; clean=true means ship it.",
    },
}
