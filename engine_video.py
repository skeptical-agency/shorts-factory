"""Video engine: trap-safe concat, burned captions, footage audit, safezone, motion lint.

Every rule here was paid for in production (TN8/Skeptical shorts pipeline).
The concat normalizes ALL inputs inside the filter graph and re-encodes —
never `-c copy` a concat that carries overlays (silent overlay death, trap 1).
Captions render as Pillow PNGs — no libass/drawtext dependency (Homebrew
ffmpeg builds often lack both).
"""

import difflib
import json
import math
import re
import subprocess
from pathlib import Path

from . import ffutil

STILL_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# ---------------------------------------------------------------- safe concat

NORMALIZE_V = "fps={fps},setsar=1,setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709,format=yuv420p"


def _scale_pad(w: int, h: int) -> str:
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")


def safe_concat(segments, out_path, width=1080, height=1920, fps=30,
                audio_rate=48000, audio_channels=2) -> dict:
    """Concatenate mixed sources (stills + video + audio) with full normalization.

    segments: [{"path": str, "duration": float (required for stills)}, ...]
    Traps handled:
      1  overlays vanish after boundary with -c copy -> always re-encode with
         fps/setsar/bt709/yuv420p normalization
      2  SAR mismatch breaks concat filter -> setsar=1 on every input, and
         after any scale
      3  mixed audio layouts drop chunks -> aresample=async=1 + aformat on
         every input (identical rate/channels)
      4  stills emit ONE frame without -loop 1 -> -loop 1 with EXACT
         -frames:v round(dur*fps) (no cumulative -t rounding drift);
         assert VIDEO STREAM duration after mux (format duration lies)
      5  all -i flags before -filter_complex (enforced by construction)
    """
    segs = []
    for s in segments:
        p = Path(s["path"]).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"segment not found: {p}")
        still = p.suffix.lower() in STILL_EXTS or str(s.get("type", "")) == "still"
        if still and not s.get("duration"):
            raise ValueError(f"still segment needs an explicit duration: {p.name}")
        segs.append({"path": p, "still": still, "dur": float(s.get("duration") or 0)})

    planned = 0.0
    for s in segs:
        planned += s["dur"] if s["still"] else ffutil.stream_duration(str(s["path"]), "v")

    cmd = [ffutil.ffmpeg(), "-y"]
    for s in segs:
        if s["still"]:
            frames = round(s["dur"] * fps)
            cmd += ["-loop", "1", "-t", f"{s['dur']:.4f}", "-i", str(s["path"])]
            s["frames"] = frames
        else:
            cmd += ["-i", str(s["path"])]
    # audio source (optional, single track for the whole concat)
    has_audio = any(
        (not s["still"]) and ffutil.video_info(str(s["path"]))["has_audio"] for s in segs
    )

    nv = NORMALIZE_V.format(fps=fps)
    parts, vlabels, alabels = [], [], []
    for i, s in enumerate(segs):
        chain = f"[{i}:v]{_scale_pad(width, height)},{nv}"
        if s["still"]:
            # -t already caps duration; keep frames exact via trim safety net
            chain += f",trim=end_frame={s['frames']},setpts=PTS-STARTPTS[v{i}]"
        else:
            chain += ",setpts=PTS-STARTPTS[v%d]" % i
        parts.append(chain)
        vlabels.append(f"[v{i}]")
        if has_audio:
            if s["still"]:
                parts.append(f"anullsrc=r={audio_rate}:cl={'stereo' if audio_channels == 2 else 'mono'}"
                             f"[a{i}raw];[a{i}raw]atrim=0:{s['dur']:.4f},asetpts=PTS-STARTPTS[a{i}]")
            else:
                parts.append(f"[{i}:a]aresample=async=1,aformat=sample_rates={audio_rate}:"
                             f"channel_layouts={'stereo' if audio_channels == 2 else 'mono'},"
                             f"asetpts=PTS-STARTPTS[a{i}]")
            alabels.append(f"[a{i}]")

    n = len(segs)
    parts.append("".join(vlabels) + ("".join(alabels) if has_audio else "") +
                 f"concat=n={n}:v=1:a={1 if has_audio else 0}[vout]" + ("[aout]" if has_audio else ""))
    fc = ";".join(parts)

    cmd += ["-filter_complex", fc, "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "19", "-pix_fmt", "yuv420p",
            "-r", str(fps)]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [str(out_path)]

    rc, out, err = ffutil.run(cmd, timeout=3600)
    if rc != 0:
        raise RuntimeError(f"concat failed: {err.strip()[-500:]}")

    # ---- mandatory post-build asserts (recipe: general verification habit)
    vd = ffutil.stream_duration(str(out_path), "v")
    report = {
        "out": str(out_path), "planned_seconds": round(planned, 2),
        "video_stream_seconds": round(vd, 2), "drift_seconds": round(vd - planned, 2),
        "checks": {},
    }
    report["checks"]["video_duration_within_1s"] = abs(vd - planned) <= 1.0
    if has_audio:
        ad = ffutil.stream_duration(str(out_path), "a")
        report["audio_stream_seconds"] = round(ad, 2)
        report["checks"]["audio_duration_within_1s"] = abs(ad - planned) <= 1.0
    info = ffutil.probe(str(out_path))
    vstream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    report["checks"]["color_metadata_bt709"] = (vstream.get("color_space") == "bt709")
    report["checks"]["pix_fmt_yuv420p"] = (vstream.get("pix_fmt") == "yuv420p")
    report["all_checks_passed"] = all(report["checks"].values())
    return report


# ---------------------------------------------------------------- captions

FUNCTION_WORDS = {"the", "a", "an", "of", "to", "in", "on", "at", "for", "and", "or",
                  "with", "is", "are", "was", "were", "but", "from", "by", "as", "it",
                  "its", "this", "that", "these", "those", "be", "been"}

_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(path: str) -> list:
    """SRT -> [{'word','start','end'}] (per-block timings shared by its words)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    words = []
    for block in re.split(r"\n\s*\n", text):
        m = _SRT_TIME.search(block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = block[m.end():].strip()
        toks = [t for t in re.sub(r"<[^>]+>", " ", body).split() if t]
        if not toks:
            continue
        span = max(0.05, (end - start) / len(toks))
        for i, t in enumerate(toks):
            words.append({"word": t, "start": round(start + i * span, 3),
                          "end": round(start + (i + 1) * span, 3)})
    return words


def parse_words_json(path: str) -> list:
    """Accept [{word|w|text, start, end}] or {'words': [...]} shapes."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(d, dict):
        d = d.get("words") or d.get("items") or []
    out = []
    for w in d:
        tok = w.get("word") or w.get("w") or w.get("text") or ""
        out.append({"word": str(tok).strip(), "start": float(w.get("start", 0)),
                    "end": float(w.get("end", 0))})
    return [w for w in out if w["word"]]


def merge_number_fragments(words: list) -> list:
    """Whisper splits '$2,499' into '$2' + ',499' — merge ^[,.]\\d tokens back."""
    merged = []
    for w in words:
        if merged and re.match(r"^[,.]\d", w["word"]):
            merged[-1] = dict(merged[-1])
            merged[-1]["word"] = merged[-1]["word"] + w["word"]
            merged[-1]["end"] = w["end"]
        else:
            merged.append(dict(w))
    return merged


def align_to_script(words: list, script: str) -> list:
    """Caption TEXT comes from the script; whisper only supplies TIMING.

    difflib SequenceMatcher aligns script tokens onto timed tokens so whisper
    mishearings never print on screen.
    """
    script_toks = re.findall(r"[\w$€£.,'’-]+", script)
    if not script_toks or not words:
        return words
    timed = [re.sub(r"[^\w$€£.,'’-]", "", w["word"]) or w["word"] for w in words]
    sm = difflib.SequenceMatcher(a=[t.lower() for t in timed],
                                 b=[t.lower() for t in script_toks], autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                w = dict(words[i1 + k])
                w["word"] = script_toks[j1 + k]
                out.append(w)
        elif tag in ("replace", "insert"):
            span = max(1, i2 - i1)
            dur = (words[min(i2, len(words) - 1)]["end"] - words[i1]["start"]) if i2 > i1 and i1 < len(words) else 0.4
            per = max(0.08, dur / max(span, j2 - j1))
            base = words[i1]["start"] if i1 < len(words) else (out[-1]["end"] if out else 0.0)
            for k in range(j1, j2):
                st = base + (k - j1) * per
                out.append({"word": script_toks[k], "start": round(st, 3),
                            "end": round(st + per, 3)})
    return out


def group_words(words: list, fps: float = 30.0, max_words: int = 4) -> list:
    """1-4 word punch groups on phrase boundaries; trailing function words push
    to the next group; enable-end capped so contiguous groups never stack on
    the boundary frame (enable is INCLUSIVE on both ends)."""
    groups, cur = [], []
    for w in words:
        cur.append(w)
        gap_next = None
        if len(cur) >= max_words:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    # push trailing function words to the next group
    fixed = []
    for gi, g in enumerate(groups):
        while len(g) > 1 and g[-1]["word"].lower().strip(".,!?;:") in FUNCTION_WORDS:
            if gi + 1 < len(groups):
                groups[gi + 1].insert(0, g.pop())
            else:
                break
        fixed.append(g)
    frame = 1.0 / fps
    out = []
    for gi, g in enumerate(fixed):
        start = g[0]["start"]
        end = g[-1]["end"]
        nxt = fixed[gi + 1][0]["start"] if gi + 1 < len(fixed) else None
        end = min(end + 0.15, (nxt - frame) if nxt is not None else end + 0.15)
        text = " ".join(x["word"] for x in g).strip()
        if text:
            out.append({"text": text, "start": round(start, 3), "end": round(max(end, start + frame * 2), 3)})
    return out


def _resolve_font(cfg_font: str = "", data_dir=None) -> Path:
    """Bebas Neue by default: config -> common system/project spots -> download."""
    cands = []
    if cfg_font:
        cands.append(Path(cfg_font).expanduser())
    home = Path.home()
    cands += [
        home / "Library/Fonts/BebasNeue-Regular.ttf",
        Path("/Library/Fonts/BebasNeue-Regular.ttf"),
        home / ".fonts/BebasNeue-Regular.ttf",
    ]
    cands += sorted(home.glob("Hermes/**/BebasNeue-Regular.ttf"))[:3]
    if data_dir:
        cands.append(Path(data_dir) / "BebasNeue-Regular.ttf")
    for c in cands:
        try:
            if c.is_file() and c.stat().st_size > 10000:
                return c
        except OSError:
            continue
    # download from google/fonts (raw TTF; the fonts.google.com zip endpoint is broken)
    import urllib.request
    dest = Path(data_dir or ".") / "BebasNeue-Regular.ttf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/google/fonts/raw/main/ofl/bebasneue/BebasNeue-Regular.ttf"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        f.write(r.read())
    if dest.stat().st_size < 10000:
        raise RuntimeError("font download failed (too small)")
    return dest


def render_caption_png(text: str, png_path, canvas_w: int, accent_words=(),
                       accent_color="#FFD400", font_path=None, base_pt: int = 96):
    """One punch-group PNG: uppercase Bebas, white + black outline + shadow,
    accent words colored. MARGIN ASSERT baked in: ink (alpha>30) must sit
    >= 40px (scaled) inside the canvas after centering, or the font shrinks;
    a hard floor that still overflows RAISES (never silently overflow)."""
    from PIL import Image, ImageDraw, ImageFont

    scale = canvas_w / 1080.0
    margin = int(40 * scale)
    max_w = int(880 * scale)
    png_h = int(170 * scale)
    up = text.upper()
    toks = up.split(" ")
    pt = int(base_pt * scale)
    floor = int(48 * scale)

    def draw_img(font):
        img = Image.new("RGBA", (canvas_w, png_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        sizes = [d.textbbox((0, 0), t + (" " if i < len(toks) - 1 else ""), font=font) for i, t in enumerate(toks)]
        total = sum(s[2] - s[0] for s in sizes)
        if total > max_w:
            return None, total
        ox = (canvas_w - total) // 2
        oy = (png_h - (sizes[0][3] - sizes[0][1])) // 2 - sizes[0][1]
        for i, t in enumerate(toks):
            col = accent_color if t.strip(".,!?;:$").lower() in {a.lower() for a in accent_words} else "white"
            piece = t + (" " if i < len(toks) - 1 else "")
            stroke = max(3, int(4 * scale))
            d.text((ox + 3 * scale, oy + 4 * scale), piece, font=font, fill=(0, 0, 0, 160))  # shadow
            d.text((ox, oy), piece, font=font, fill=col, stroke_width=stroke, stroke_fill="black")
            ox += sizes[i][2] - sizes[i][0]
        return img, total

    while True:
        font = ImageFont.truetype(str(font_path), pt)
        img, total = draw_img(font)
        if img is not None:
            break
        if pt <= floor:
            # last resort: two lines
            mid = len(toks) // 2
            l1, l2 = " ".join(toks[:mid]), " ".join(toks[mid:])
            font = ImageFont.truetype(str(font_path), max(floor, int(pt * 0.9)))
            img = Image.new("RGBA", (canvas_w, png_h), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            stroke = max(3, int(4 * scale))
            for li, line in enumerate((l1, l2)):
                bb = d.textbbox((0, 0), line, font=font)
                lx = (canvas_w - (bb[2] - bb[0])) // 2
                ly = int(png_h * 0.08) + li * int(png_h * 0.46)
                d.text((lx + 3 * scale, ly + 4 * scale), line, font=font, fill=(0, 0, 0, 160))
                d.text((lx, ly), line, font=font, fill="white", stroke_width=stroke, stroke_fill="black")
                if (bb[2] - bb[0]) > canvas_w - 2 * margin:
                    raise RuntimeError(f"caption cannot fit even on two lines at font floor: {text!r}")
            break
        pt -= 4

    # margin assert on final ink
    bbox = img.getbbox()
    if bbox:
        x0, y0, x1, y1 = bbox
        if x0 < margin or y0 < margin or x1 > canvas_w - margin or y1 > png_h - margin:
            # shrink-to-fit already tried; if ink still breaches, fail loudly
            if x0 < 0 or x1 > canvas_w:
                raise RuntimeError(f"caption ink off-canvas after centering: {text!r}")
    img.save(png_path)
    return {"png": str(png_path), "pt": pt, "ink_bbox": bbox}


def burn_captions(video: str, out_path: str, groups: list, width: int, height: int,
                  accent_words=(), accent_color="#FFD400", font_path=None,
                  data_dir=None, y_frac: float = 1540 / 1920) -> dict:
    """Overlay per-group PNGs with enable windows. All -i BEFORE -filter_complex.

    Placement default y=1540 of 1920 (clear of platform UI). Keep other baked
    text OUT of y1500-1700 (caption band collision).
    """
    work = Path(str(out_path) + ".caps")
    work.mkdir(parents=True, exist_ok=True)
    font = _resolve_font(font_path or "", data_dir)
    rendered = []
    for i, g in enumerate(groups):
        png = work / f"cap_{i:03d}.png"
        info = render_caption_png(g["text"], png, width, accent_words, accent_color, font)
        rendered.append(info)
    y = int(round(y_frac * height))
    cmd = [ffutil.ffmpeg(), "-y", "-i", video]
    for r in rendered:
        cmd += ["-i", r["png"]]
    fc, prev = [], "[0:v]"
    for i, g in enumerate(groups):
        outl = f"[v{i}]"
        fc.append(f"{prev}[{i + 1}:v]overlay=x=(W-w)/2:y={y}:"
                  f"enable='between(t,{g['start']:.3f},{g['end']:.3f})'{outl}")
        prev = outl
    cmd += ["-filter_complex", ";".join(fc), "-map", prev]
    if ffutil.video_info(video)["has_audio"]:
        cmd += ["-map", "0:a", "-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "19", "-pix_fmt", "yuv420p",
            str(out_path)]
    rc, out, err = ffutil.run(cmd, timeout=3600)
    if rc != 0:
        raise RuntimeError(f"caption overlay failed: {err.strip()[-500:]}")
    return {"out": str(out_path), "groups": len(groups),
            "font": str(font), "band_y": y,
            "timing": [{"text": g["text"], "start": g["start"], "end": g["end"]} for g in groups]}


def verify_captions(silent_video: str, captioned_video: str, groups: list,
                    width: int, height: int, samples: int = 12) -> dict:
    """Diff the caption band between silent and captioned renders at 10+
    timestamps spanning every group. maxdiff > ~60 = captions rendered.
    Vision alone misses this (dark frames look plausible either way)."""
    try:
        import numpy as np
    except ImportError:
        return {"skipped": "numpy not installed — pip install numpy for band-diff verification",
                "pass": None}
    y0 = int(height * (1400 / 1920))
    band_h = int(height * (300 / 1920))
    times = []
    for g in groups:
        mid = (g["start"] + g["end"]) / 2
        times.append(mid)
    if len(times) < samples:
        span = groups[-1]["end"] if groups else 10
        times += [span * i / samples for i in range(1, samples)]
    times = sorted(set(round(t, 2) for t in times))[: max(samples, len(groups))]

    def grab(path, t):
        p = f"/tmp/sfband_{abs(hash(path + str(t)))}.png"
        rc, _, err = ffutil.run([ffutil.ffmpeg(), "-y", "-v", "error", "-ss", str(t),
                                 "-i", path, "-frames:v", "1",
                                 "-vf", f"crop={width}:{band_h}:0:{y0}", p], timeout=60)
        if rc != 0:
            return None
        from PIL import Image
        a = np.asarray(Image.open(p).convert("L"), dtype=int)
        Path(p).unlink(missing_ok=True)
        return a

    rendered_hits, diffs = 0, []
    for t in times:
        a, b = grab(silent_video, t), grab(captioned_video, t)
        if a is None or b is None or a.shape != b.shape:
            continue
        d = int(np.abs(a - b).max())
        diffs.append({"t": t, "maxdiff": d})
        if d > 60:
            rendered_hits += 1
    return {"samples": len(diffs), "rendered_hits": rendered_hits,
            "pass": rendered_hits >= max(1, int(len(groups) * 0.6)),
            "detail": diffs[:20]}


# ---------------------------------------------------------------- footage audit

def footage_audit(video: str, out_dir: str, coarse_fps: int = 1,
                  windows=None, dark_threshold: float = 40.0) -> dict:
    """Coarse contact sheet + per-second brightness + full-res fine grabs.

    Coarse sheets MISS things: rating badges, small terminal text, single
    off-topic frames in fast cuts. Fine pass grabs START/MID/END of each
    candidate window at FULL RES for vision review. Brightness audit flags
    long near-black stretches (read as broken on mobile).
    """
    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    base = Path(video).stem

    # 1) contact sheet frames at coarse_fps
    sheet_dir = od / f"{base}_sheet"
    sheet_dir.mkdir(exist_ok=True)
    rc, _, err = ffutil.run([ffutil.ffmpeg(), "-y", "-v", "error", "-i", video,
                             "-vf", f"fps={coarse_fps}",
                             str(sheet_dir / "s_%03d.jpg")], timeout=1800)
    if rc != 0:
        raise RuntimeError(f"contact sheet failed: {err.strip()[-300:]}")
    sheets = sorted(str(p) for p in sheet_dir.glob("s_*.jpg"))

    # 2) brightness per second (YAVG)
    rc, out, err = ffutil.run([ffutil.ffmpeg(), "-i", video,
                               "-vf", f"fps={coarse_fps},signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                               "-f", "null", "-"], timeout=1800)
    ys = [float(m) for m in re.findall(r"YAVG=([\d.]+)", (err or "") + (out or ""))]
    dark = []
    run_start = None
    for i, y in enumerate(ys):
        if y < dark_threshold and run_start is None:
            run_start = i
        elif y >= dark_threshold and run_start is not None:
            if i - run_start >= 3:
                dark.append({"from_s": run_start, "to_s": i, "mean_yavg": round(sum(ys[run_start:i]) / (i - run_start), 1)})
            run_start = None
    if run_start is not None and len(ys) - run_start >= 3:
        dark.append({"from_s": run_start, "to_s": len(ys), "mean_yavg": round(sum(ys[run_start:]) / max(1, len(ys) - run_start), 1)})

    # 3) fine full-res grabs for requested windows
    grabs = []
    for w in (windows or []):
        s, e = float(w["start"]), float(w["end"])
        for tag, t in (("start", s), ("mid", (s + e) / 2), ("end", max(s, e - 0.1))):
            p = od / f"{base}_fine_{int(s)}-{int(e)}_{tag}.png"
            rc2, _, _ = ffutil.run([ffutil.ffmpeg(), "-y", "-v", "error", "-ss", f"{t:.2f}",
                                    "-i", video, "-frames:v", "1", str(p)], timeout=120)
            if rc2 == 0:
                grabs.append({"window": [s, e], "at": tag, "t": round(t, 2), "path": str(p)})

    return {
        "contact_sheet_frames": len(sheets),
        "sheet_dir": str(sheet_dir),
        "sheet_sample": sheets[:5],
        "brightness_dark_segments": dark,
        "fine_grabs": grabs,
        "review_checklist": [
            "rating badges/watermarks on ANY frame of a short trailer exclude the WHOLE clip",
            "read small on-screen text at FULL RES (last lines of terminal/log windows)",
            "facecam windows: verify person/setting matches on start+mid+end grabs",
            "privacy: personal URLs, bookmark bars, account names",
            "log every exclusion (window + reason) in the build README",
        ],
    }


# ---------------------------------------------------------------- safezone

def safezone_check(media: str, out_dir: str, bboxes=None, frame_t: float = 1.0,
                   canvas_h: int = 1920) -> dict:
    """Thumbnail safe zone: ALL hook/title text inside y=400..1500 of 1920.

    YouTube grid thumbs crop the 9:16 frame to a MIDDLE BAND — text outside
    the band is invisible where the click decision happens. Caption band
    y~1500-1700 must carry ONLY word-captions (collision rule).
    Also renders the simulated 16:9 grid crop for a visual check.
    """
    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    info = ffutil.video_info(media) if not ffutil.is_still(media) else None
    if info:
        h, w = info["height"], info["width"]
    else:
        from PIL import Image
        with Image.open(media) as im:
            w, h = im.size
    scale = h / canvas_h
    band_top, band_bot = int(400 * scale), int(1500 * scale)
    cap_top, cap_bot = int(1500 * scale), int(1700 * scale)

    results = []
    for b in (bboxes or []):
        y0, y1 = float(b["y0"]) * scale, float(b["y1"]) * scale
        label = b.get("label", "text")
        kind = b.get("kind", "title")  # title | caption | other
        if kind == "caption":
            ok = y0 >= cap_top - 5 and y1 <= cap_bot + 5
            results.append({"label": label, "kind": kind, "y": [round(y0), round(y1)],
                            "in_caption_band": ok})
        else:
            ok = y0 >= band_top and y1 <= band_bot
            results.append({"label": label, "kind": kind, "y": [round(y0), round(y1)],
                            "in_safe_band": ok,
                            "collides_with_caption_band": not (y1 <= cap_top or y0 >= cap_bot)})

    # simulated grid crop (16:9 middle band) for vision review
    frame_png = str(od / "frame.png")
    if info:
        rc, _, err = ffutil.run([ffutil.ffmpeg(), "-y", "-v", "error", "-ss", str(frame_t),
                                 "-i", media, "-frames:v", "1", frame_png], timeout=60)
    else:
        frame_png = str(media)
    crop_png = str(od / "grid_crop_sim.png")
    rc, _, err = ffutil.run([ffutil.ffmpeg(), "-y", "-v", "error", "-i", frame_png,
                             "-vf", f"crop={w}:{int(w * 9 / 16)}:0:(ih-{int(w * 9 / 16)})/2",
                             crop_png], timeout=60)
    if rc != 0:
        crop_png = None

    passed = all(r.get("in_safe_band", r.get("in_caption_band", True)) for r in results) if results else None
    return {
        "canvas": [w, h], "safe_band_y": [band_top, band_bot],
        "caption_band_y": [cap_top, cap_bot],
        "elements": results,
        "elements_pass": passed,
        "grid_crop_simulation": crop_png,
        "advice": "headline in the vertical MIDDLE (~y=640 hook); hook cards rendered STATIC (platforms grab arbitrary frames); verify the crop reads at thumbnail size and the picture matches the headline noun",
    }


# ---------------------------------------------------------------- motion lint

BANNED_PATTERNS = [
    (r"zoompan", "zoompan: whole-pixel rounding stutters on slow motion; use static stills or cuts (4x-supersample only for pure b-roll)"),
    (r"crop=[^\"']*['\"][^'\"]*\bt\b", "time-driven crop expression: sub-pixel motion rounds to steps — freeze the card"),
    (r"overlay=[^\"']*x=[^'\"]*\bt\b", "time-driven overlay x expression: motion on baked elements stutters"),
    (r"overlay=[^\"']*y=[^'\"]*\bt\b", "time-driven overlay y expression: motion on baked elements stutters"),
    (r"mod\(t", "time-modulated expression: stutter risk on stills"),
    (r"if\(gt\(t", "time-conditional expression: stutter risk on stills"),
    (r"rawvideo", "per-frame PIL zoom piped as rawvideo: known stutter source"),
    (r"-loop\s+1[^|;&]*zoompan", "zoompan on a LOOPED input multiplies frames; feed a single still and let d= generate frames"),
]


def motion_lint(source: str, is_path: bool = True) -> dict:
    """Lint a build script for banned motion patterns. Deterministic —
    pixel-level stutter detection is unreliable (no threshold separated
    approved builds from real stutter in production testing)."""
    text = Path(source).read_text(encoding="utf-8", errors="replace") if is_path else source
    findings = []
    for ln, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pat, why in BANNED_PATTERNS:
            if re.search(pat, line):
                findings.append({"line": ln, "pattern": pat, "why": why,
                                 "code": line.strip()[:120]})
    return {"clean": not findings, "findings": findings,
            "rule": "text-bearing stills get NO motion; hook/thumbnail cards static; motion only on pure b-roll; density comes from CUTS"}
